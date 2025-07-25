import logging
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Self

from pydantic import (
    BaseModel,
    Field,
    root_validator,
)

from reconcile import queries
from reconcile.gql_definitions.common.clusters import (
    ClusterMachinePoolV1,
    ClusterSpecAutoScaleV1,
    ClusterV1,
)
from reconcile.typed_queries.clusters import get_clusters
from reconcile.utils.differ import diff_mappings
from reconcile.utils.disabled_integrations import integration_is_enabled
from reconcile.utils.ocm import (
    DEFAULT_OCM_MACHINE_POOL_ID,
    OCM,
    OCMMap,
)

QONTRACT_INTEGRATION = "ocm-machine-pools"


class ClusterType(Enum):
    OSD = "osd"
    ROSA_CLASSIC = "rosa"
    ROSA_HCP = "hypershift"


class InvalidUpdateError(Exception):
    pass


class AbstractAutoscaling(BaseModel):
    def has_diff(self, autoscale: ClusterSpecAutoScaleV1) -> bool:
        return (
            self.get_min() != autoscale.min_replicas
            or self.get_max() != autoscale.max_replicas
        )

    @abstractmethod
    def get_min(self) -> int:
        pass

    @abstractmethod
    def get_max(self) -> int:
        pass


class MachinePoolAutoscaling(AbstractAutoscaling):
    min_replicas: int
    max_replicas: int

    @root_validator()
    def max_greater_min(cls, field_values: Mapping[str, Any]) -> Mapping[str, Any]:
        min_replicas = field_values.get("min_replicas")
        max_replicas = field_values.get("max_replicas")
        if min_replicas is None or max_replicas is None:
            raise ValueError("min_replicas and max_replicas must be set")
        if min_replicas > max_replicas:
            raise ValueError("max_replicas must be greater than min_replicas")
        return field_values

    def get_min(self) -> int:
        return self.min_replicas

    def get_max(self) -> int:
        return self.max_replicas


class NodePoolAutoscaling(AbstractAutoscaling):
    min_replica: int
    max_replica: int

    @root_validator()
    def max_greater_min(cls, field_values: Mapping[str, Any]) -> Mapping[str, Any]:
        min_replica = field_values.get("min_replica")
        max_replica = field_values.get("max_replica")
        if min_replica is None or max_replica is None:
            raise ValueError("min_replica and max_replica must be set")
        if min_replica > max_replica:
            raise ValueError("max_replica must be greater than min_replica")
        return field_values

    def get_min(self) -> int:
        return self.min_replica

    def get_max(self) -> int:
        return self.max_replica


class AbstractPool(ABC, BaseModel):
    # Abstract class for machine pools, to be implemented by OSD/HyperShift classes

    id: str
    replicas: int | None
    taints: list[Mapping[str, str]] | None
    labels: Mapping[str, str] | None
    cluster: str
    cluster_type: ClusterType = Field(..., exclude=True)
    autoscaling: AbstractAutoscaling | None

    @root_validator()
    def validate_scaling(cls, field_values: Mapping[str, Any]) -> Mapping[str, Any]:
        if field_values.get("autoscaling") and field_values.get("replicas"):
            raise ValueError("autoscaling and replicas are mutually exclusive")
        return field_values

    @abstractmethod
    def create(self, ocm: OCM) -> None:
        pass

    @abstractmethod
    def delete(self, ocm: OCM) -> None:
        pass

    @abstractmethod
    def update(self, ocm: OCM) -> None:
        pass

    @abstractmethod
    def has_diff(self, pool: ClusterMachinePoolV1) -> bool:
        pass

    @abstractmethod
    def invalid_diff(self, pool: ClusterMachinePoolV1) -> str | None:
        pass

    @abstractmethod
    def deletable(self) -> bool:
        pass

    def _has_diff_autoscale(self, pool: ClusterMachinePoolV1) -> bool:
        if self.autoscaling is None and pool.autoscale is None:
            return False
        if self.autoscaling is None or pool.autoscale is None:
            return True
        return self.autoscaling.has_diff(pool.autoscale)


class MachinePool(AbstractPool):
    # Machine pool, used for OSD clusters

    instance_type: str

    def delete(self, ocm: OCM) -> None:
        ocm.delete_machine_pool(self.cluster, self.dict(by_alias=True))

    def create(self, ocm: OCM) -> None:
        ocm.create_machine_pool(self.cluster, self.dict(by_alias=True))

    def update(self, ocm: OCM) -> None:
        update_dict = self.dict(by_alias=True)
        # can not update instance_type
        del update_dict["instance_type"]
        if not update_dict["labels"]:
            del update_dict["labels"]
        if not update_dict["taints"]:
            del update_dict["taints"]
        ocm.update_machine_pool(self.cluster, update_dict)

    def has_diff(self, pool: ClusterMachinePoolV1) -> bool:
        if self.taints != pool.taints or self.labels != pool.labels:
            logging.warning(
                f"updating labels or taints for machine pool {pool.q_id} "
                f"will only be applied to new Nodes"
            )

        return (
            self.replicas != pool.replicas
            or self.taints != pool.taints
            or self.labels != pool.labels
            or self.instance_type != pool.instance_type
            or self._has_diff_autoscale(pool)
        )

    def invalid_diff(self, pool: ClusterMachinePoolV1) -> str | None:
        if self.instance_type != pool.instance_type:
            return "instance_type"
        return None

    def deletable(self) -> bool:
        # OSD NON CCS clusters can't delete default worker machine pool
        return not (
            self.cluster_type == ClusterType.OSD
            and self.id == DEFAULT_OCM_MACHINE_POOL_ID
        )

    @classmethod
    def create_from_gql(
        cls,
        pool: ClusterMachinePoolV1,
        cluster: str,
        cluster_type: ClusterType,
    ) -> Self:
        autoscaling: MachinePoolAutoscaling | None = None
        if pool.autoscale:
            autoscaling = MachinePoolAutoscaling(
                min_replicas=pool.autoscale.min_replicas,
                max_replicas=pool.autoscale.max_replicas,
            )
        return cls(
            id=pool.q_id,
            replicas=pool.replicas,
            autoscaling=autoscaling,
            instance_type=pool.instance_type,
            taints=[p.dict(by_alias=True) for p in pool.taints or []],
            labels=pool.labels,
            cluster=cluster,
            cluster_type=cluster_type,
        )


class AWSNodePool(BaseModel):
    instance_type: str


class NodePool(AbstractPool):
    # Node pool, used for HyperShift clusters

    aws_node_pool: AWSNodePool
    subnet: str | None

    def delete(self, ocm: OCM) -> None:
        ocm.delete_node_pool(self.cluster, self.dict(by_alias=True))

    def create(self, ocm: OCM) -> None:
        spec = self.dict(by_alias=True)
        ocm.create_node_pool(self.cluster, spec)

    def update(self, ocm: OCM) -> None:
        update_dict = self.dict(by_alias=True)
        # can not update instance_type
        del update_dict["aws_node_pool"]
        # can not update subnet
        del update_dict["subnet"]
        if not update_dict["labels"]:
            del update_dict["labels"]
        if not update_dict["taints"]:
            del update_dict["taints"]
        ocm.update_node_pool(self.cluster, update_dict)

    def has_diff(self, pool: ClusterMachinePoolV1) -> bool:
        if self.taints != pool.taints or self.labels != pool.labels:
            logging.warning(
                f"updating labels or taints for node pool {pool.q_id} "
                f"will only be applied to new Nodes"
            )

        return (
            self.replicas != pool.replicas
            or self.taints != pool.taints
            or self.labels != pool.labels
            or self.aws_node_pool.instance_type != pool.instance_type
            or self.subnet != pool.subnet
            or self._has_diff_autoscale(pool)
        )

    def invalid_diff(self, pool: ClusterMachinePoolV1) -> str | None:
        if self.aws_node_pool.instance_type != pool.instance_type:
            return "instance_type"
        if self.subnet != pool.subnet:
            return "subnet"
        return None

    def deletable(self) -> bool:
        return True

    @classmethod
    def create_from_gql(
        cls,
        pool: ClusterMachinePoolV1,
        cluster: str,
        cluster_type: ClusterType,
    ) -> Self:
        autoscaling: NodePoolAutoscaling | None = None
        if pool.autoscale:
            autoscaling = NodePoolAutoscaling(
                min_replica=pool.autoscale.min_replicas,
                max_replica=pool.autoscale.max_replicas,
            )

        return cls(
            id=pool.q_id,
            replicas=pool.replicas,
            autoscaling=autoscaling,
            aws_node_pool=AWSNodePool(
                instance_type=pool.instance_type,
            ),
            taints=[p.dict(by_alias=True) for p in pool.taints or []],
            labels=pool.labels,
            subnet=pool.subnet,
            cluster=cluster,
            cluster_type=cluster_type,
        )


class PoolHandler(BaseModel):
    # Class, that acts on a pool, based on the action

    action: str
    pool: AbstractPool

    def act(self, dry_run: bool, ocm: OCM) -> None:
        logging.info(f"{self.action} {self.pool.dict(by_alias=True)}")
        if dry_run:
            return

        if not self.action:
            pass
        elif self.action == "delete":
            self.pool.delete(ocm)
        elif self.action == "create":
            self.pool.create(ocm)
        elif self.action == "update":
            self.pool.update(ocm)


class DesiredMachinePool(BaseModel):
    cluster_name: str
    cluster_type: ClusterType
    pools: list[ClusterMachinePoolV1]

    def build_pool_handler(
        self,
        action: str,
        pool: ClusterMachinePoolV1,
    ) -> PoolHandler:
        pool_builder = (
            NodePool.create_from_gql
            if self.cluster_type == ClusterType.ROSA_HCP
            else MachinePool.create_from_gql
        )
        return PoolHandler(
            action=action,
            pool=pool_builder(pool, self.cluster_name, self.cluster_type),
        )


def fetch_current_state(
    ocm_map: OCMMap,
    clusters: Iterable[ClusterV1],
) -> Mapping[str, list[AbstractPool]]:
    return {
        c.name: fetch_current_state_for_cluster(c, ocm_map.get(c.name))
        for c in clusters
    }


def _classify_cluster_type(cluster: ClusterV1) -> ClusterType:
    if cluster.spec is None:
        raise ValueError(f"cluster {cluster.name} is missing spec")
    match cluster.spec.product:
        case "osd":
            return ClusterType.OSD
        case "rosa":
            return (
                ClusterType.ROSA_HCP
                if cluster.spec.hypershift
                else ClusterType.ROSA_CLASSIC
            )
        case _:
            raise ValueError(f"unknown cluster type for cluster {cluster.name}")


def fetch_current_state_for_cluster(cluster: ClusterV1, ocm: OCM) -> list[AbstractPool]:
    cluster_type = _classify_cluster_type(cluster)
    if cluster_type == ClusterType.ROSA_HCP:
        return [
            NodePool(
                id=node_pool["id"],
                replicas=node_pool.get("replicas"),
                autoscaling=NodePoolAutoscaling(
                    # Hypershift uses singular form
                    min_replica=node_pool["autoscaling"]["min_replica"],
                    max_replica=node_pool["autoscaling"]["max_replica"],
                )
                if node_pool.get("autoscaling")
                else None,
                aws_node_pool=AWSNodePool(
                    instance_type=node_pool["aws_node_pool"]["instance_type"]
                ),
                taints=node_pool.get("taints"),
                labels=node_pool.get("labels"),
                subnet=node_pool.get("subnet"),
                cluster=cluster.name,
                cluster_type=cluster_type,
            )
            for node_pool in ocm.get_node_pools(cluster.name)
        ]
    return [
        MachinePool(
            id=machine_pool["id"],
            replicas=machine_pool.get("replicas"),
            autoscaling=MachinePoolAutoscaling(
                min_replicas=machine_pool["autoscaling"]["min_replicas"],
                max_replicas=machine_pool["autoscaling"]["max_replicas"],
            )
            if machine_pool.get("autoscaling")
            else None,
            instance_type=machine_pool["instance_type"],
            taints=machine_pool.get("taints"),
            labels=machine_pool.get("labels"),
            cluster=cluster.name,
            cluster_type=cluster_type,
        )
        for machine_pool in ocm.get_machine_pools(cluster.name)
    ]


def create_desired_state_from_gql(
    clusters: Iterable[ClusterV1],
) -> dict[str, DesiredMachinePool]:
    return {
        cluster.name: DesiredMachinePool(
            cluster_name=cluster.name,
            cluster_type=_classify_cluster_type(cluster),
            pools=cluster.machine_pools,
        )
        for cluster in clusters
        if cluster.machine_pools is not None
    }


def calculate_diff(
    current_state: Mapping[str, list[AbstractPool]],
    desired_state: Mapping[str, DesiredMachinePool],
) -> tuple[list[PoolHandler], list[InvalidUpdateError]]:
    current_machine_pools = {
        (cluster_name, machine_pool.id): machine_pool
        for cluster_name, machine_pools in current_state.items()
        for machine_pool in machine_pools
    }

    desired_machine_pools = {
        (desired.cluster_name, desired_machine_pool.q_id): desired_machine_pool
        for desired in desired_state.values()
        for desired_machine_pool in desired.pools
    }

    diff_result = diff_mappings(
        current_machine_pools,
        desired_machine_pools,
        equal=lambda current, desired: not current.has_diff(desired),
    )

    diffs: list[PoolHandler] = []
    errors: list[InvalidUpdateError] = []

    for (cluster_name, _), desired_machine_pool in diff_result.add.items():
        diffs.append(
            desired_state[cluster_name].build_pool_handler(
                "create",
                desired_machine_pool,
            )
        )
    for (cluster_name, _), diff_pair in diff_result.change.items():
        invalid_diff = diff_pair.current.invalid_diff(diff_pair.desired)
        if invalid_diff:
            errors.append(
                InvalidUpdateError(
                    f"can not update {invalid_diff} for existing machine pool on cluster {cluster_name}, CURRENT: {diff_pair.current.json()}, DESIRED: {diff_pair.desired.json()}"
                )
            )
        else:
            diffs.append(
                desired_state[cluster_name].build_pool_handler(
                    "update",
                    diff_pair.desired,
                )
            )

    for (cluster_name, _), current_machine_pool in diff_result.delete.items():
        if not desired_state[cluster_name].pools:
            errors.append(
                InvalidUpdateError(
                    f"can not delete all machine pools for cluster {cluster_name}"
                )
            )
        elif current_machine_pool.deletable():
            diffs.append(
                PoolHandler(
                    action="delete",
                    pool=current_machine_pool,
                )
            )
        else:
            errors.append(
                InvalidUpdateError(
                    f"can not delete machine pool {current_machine_pool.id} for cluster {cluster_name}"
                )
            )

    return diffs, errors


def act(dry_run: bool, diffs: Iterable[PoolHandler], ocm_map: OCMMap) -> None:
    for diff in diffs:
        logging.info([diff.action, diff.pool.cluster, diff.pool.id])
        if not dry_run:
            ocm = ocm_map.get(diff.pool.cluster)
            if ocm is not None:
                diff.act(dry_run, ocm)


def _cluster_is_compatible(cluster: ClusterV1) -> bool:
    return cluster.ocm is not None and cluster.machine_pools is not None


def run(dry_run: bool) -> None:
    clusters = get_clusters()

    filtered_clusters = [
        c
        for c in clusters
        if integration_is_enabled(QONTRACT_INTEGRATION, c) and _cluster_is_compatible(c)
    ]
    if not filtered_clusters:
        logging.debug("No machinePools definitions found in app-interface")
        return

    settings = queries.get_app_interface_settings()
    cluster_like_objects = [
        cluster.dict(by_alias=True) for cluster in filtered_clusters
    ]
    ocm_map = OCMMap(
        clusters=cluster_like_objects,
        integration=QONTRACT_INTEGRATION,
        settings=settings,
    )

    current_state = fetch_current_state(ocm_map, filtered_clusters)
    desired_state = create_desired_state_from_gql(filtered_clusters)
    diffs, errors = calculate_diff(current_state, desired_state)

    act(dry_run, diffs, ocm_map)

    if errors:
        for err in errors:
            logging.error(err)
        raise ExceptionGroup("InvalidUpdateErrors", errors)
