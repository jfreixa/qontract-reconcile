from collections import defaultdict
from collections.abc import (
    Generator,
    Iterable,
)
from typing import Any

from reconcile.utils.ocm.base import (
    ACTIVE_SUBSCRIPTION_STATES,
    PRODUCT_ID_OSD,
    PRODUCT_ID_ROSA,
    ClusterDetails,
    FleetManagerServiceCluster,
    OCMCluster,
    OCMClusterState,
    OCMOrganizationLabel,
    OCMSubscriptionLabel,
    ProvisionShard,
    build_label_container,
)
from reconcile.utils.ocm.labels import (
    get_labels,
    get_organization_labels,
)
from reconcile.utils.ocm.search_filters import Filter
from reconcile.utils.ocm.subscriptions import (
    build_subscription_filter,
    get_subscriptions,
)
from reconcile.utils.ocm_base_client import OCMBaseClient

NODE_POOL_DESIRED_KEYS = {
    "id",
    "instance_type",
    "replicas",
    "autoscaling",
    "labels",
    "taints",
    "aws_node_pool",
    "subnet",
    "version",
}


def discover_clusters_by_labels(
    ocm_api: OCMBaseClient, label_filter: Filter
) -> list[ClusterDetails]:
    """
    Discover clusters in OCM by their subscription and organization labels.
    The discovery labels are defined via the label_filter argument.
    """
    subscription_labels: dict[str, list[OCMSubscriptionLabel]] = defaultdict(list)
    organization_labels: dict[str, list[OCMOrganizationLabel]] = defaultdict(list)

    for label in get_labels(ocm_api=ocm_api, filter=label_filter):
        if isinstance(label, OCMSubscriptionLabel):
            subscription_labels[label.subscription_id].append(label)
        elif isinstance(label, OCMOrganizationLabel):
            organization_labels[label.organization_id].append(label)
    if not subscription_labels and not organization_labels:
        return []

    sub_id_filter = Filter().is_in("id", subscription_labels.keys())
    org_id_filter = Filter().is_in("organization_id", organization_labels.keys())
    subscription_filter = sub_id_filter | org_id_filter

    clusters = list(
        get_cluster_details_for_subscriptions(
            ocm_api=ocm_api,
            subscription_filter=subscription_filter,
        )
    )

    # fill in labels
    for cluster in clusters:
        cluster.subscription_labels = build_label_container(
            subscription_labels[cluster.ocm_cluster.subscription.id]
        )
        cluster.organization_labels = build_label_container(
            organization_labels[cluster.organization_id]
        )

    return clusters


def discover_clusters_for_subscriptions(
    ocm_api: OCMBaseClient,
    subscription_ids: list[str],
    cluster_filter: Filter | None = None,
) -> list[ClusterDetails]:
    """
    Discover clusters by filtering on their subscription IDs.
    Additionally, a cluster_filter can be applied to narrow the
    discovered clusters.
    """
    if not subscription_ids:
        return []

    return list(
        get_cluster_details_for_subscriptions(
            ocm_api=ocm_api,
            subscription_filter=Filter().is_in("id", subscription_ids),
            cluster_filter=cluster_filter,
        )
    )


def discover_clusters_for_organizations(
    ocm_api: OCMBaseClient,
    organization_ids: Iterable[str],
    cluster_filter: Filter | None = None,
) -> list[ClusterDetails]:
    """
    Discover clusters by filtering on their organization IDs.
    Additionally, a cluster_filter can be applied to narrow the
    discovered clusters.
    """
    if not organization_ids:
        return []

    return list(
        get_cluster_details_for_subscriptions(
            ocm_api=ocm_api,
            subscription_filter=Filter().is_in("organization_id", organization_ids),
            cluster_filter=cluster_filter,
        )
    )


def get_ocm_clusters(
    ocm_api: OCMBaseClient,
    cluster_filter: Filter,
) -> Generator[OCMCluster, None, None]:
    for cluster_dict in ocm_api.get_paginated(
        api_path="/api/clusters_mgmt/v1/clusters",
        params={"search": cluster_filter.render(), "order": "creation_timestamp"},
        max_page_size=100,
    ):
        yield OCMCluster(**cluster_dict)


def get_provision_shard_for_cluster_id(
    ocm_api: OCMBaseClient,
    id: str,
) -> ProvisionShard:
    data = ocm_api.get(api_path=f"/api/clusters_mgmt/v1/clusters/{id}/provision_shard")
    return ProvisionShard(**data)


def get_service_clusters(
    ocm_api: OCMBaseClient,
) -> Generator[FleetManagerServiceCluster, None, None]:
    for cluster_dict in ocm_api.get_paginated(
        api_path="/api/osd_fleet_mgmt/v1/service_clusters",
        max_page_size=100,
    ):
        if not cluster_dict.get("provision_shard_reference"):
            continue
        yield FleetManagerServiceCluster(**cluster_dict)


def get_cluster_details_for_subscriptions(
    ocm_api: OCMBaseClient,
    subscription_filter: Filter | None = None,
    cluster_filter: Filter | None = None,
    init_labels: bool = False,
) -> Generator[ClusterDetails, None, None]:
    """
    Discover clusters by filtering on their subscriptions. The subscription_filter
    can be used to restrict on any subscription field. Additionally, a cluster_filter
    can be applied to narrow the discovered clusters.
    """
    # get subscription details
    subscriptions = get_subscriptions(
        ocm_api=ocm_api,
        filter=(subscription_filter or Filter())
        & build_subscription_filter(states=ACTIVE_SUBSCRIPTION_STATES, managed=True),
    )
    if not subscriptions:
        return

    # get organization labels
    organization_labels: dict[str, list[OCMOrganizationLabel]] = defaultdict(list)
    if init_labels:
        for label in get_organization_labels(
            ocm_api=ocm_api,
            filter=Filter().is_in(
                "organization_id",
                {s.organization_id for s in subscriptions.values()},
            ),
        ):
            organization_labels[label.organization_id].append(label)

    cluster_search_filter = (cluster_ready_for_app_interface() & cluster_filter).is_in(
        "subscription.id", subscriptions.keys()
    )
    for filter_chunk in cluster_search_filter.chunk_by(
        "subscription.id", 100, ignore_missing=True
    ):
        for cluster in get_ocm_clusters(ocm_api=ocm_api, cluster_filter=filter_chunk):
            subscription = subscriptions[cluster.subscription.id]
            yield ClusterDetails(
                ocm_cluster=cluster,
                organization_id=subscription.organization_id,
                capabilities={
                    capability.name: capability
                    for capability in subscription.capabilities or []
                },
                organization_labels=build_label_container(
                    organization_labels.get(subscription.organization_id) or []
                ),
                subscription_labels=build_label_container(subscription.labels or []),
            )


def cluster_ready_for_app_interface() -> Filter:
    """
    Filter for clusters that are considered ready for app-interface processing,
    which boils down to managed OSD/ROSA clusters in ready state.
    """
    return (
        Filter()
        .eq("managed", "true")
        .eq("state", OCMClusterState.READY.value)
        .is_in("product.id", [PRODUCT_ID_OSD, PRODUCT_ID_ROSA])
    )


def get_node_pools(ocm_api: OCMBaseClient, cluster_id: str) -> list[dict[str, Any]]:
    results = []
    api = f"/api/clusters_mgmt/v1/clusters/{cluster_id}/node_pools"

    for item in ocm_api.get_paginated(api):
        result = {k: v for k, v in item.items() if k in NODE_POOL_DESIRED_KEYS}
        results.append(result)

    return results


def get_provisioning_shard_id(ocm_api: OCMBaseClient, cluster_id: str) -> str:
    api = f"/api/clusters_mgmt/v1/clusters/{cluster_id}/provision_shard"

    item = ocm_api.get(api)
    return item["id"]
