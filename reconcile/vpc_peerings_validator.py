import ipaddress
import logging
import sys
from typing import (
    cast,
)

from reconcile import queries
from reconcile.gql_definitions.vpc_peerings_validator import vpc_peerings_validator
from reconcile.gql_definitions.vpc_peerings_validator.vpc_peerings_validator import (
    ClusterPeeringConnectionAccountV1,
    ClusterPeeringConnectionAccountVPCMeshV1,
    ClusterPeeringConnectionClusterAccepterV1,
    ClusterPeeringConnectionClusterRequesterV1,
    ClusterV1,
    VpcPeeringsValidatorQueryData,
)
from reconcile.status import ExitCodes
from reconcile.utils import gql
from reconcile.utils.aws_api import AWSApi

QONTRACT_INTEGRATION = "vpc-peerings-validator"


def validate_no_cidr_overlap(
    query_data: VpcPeeringsValidatorQueryData,
) -> bool:
    clusters: list[ClusterV1] = query_data.clusters or []

    for cluster in clusters:
        if cluster.peering:
            assert cluster.network
            peerings_entries = [
                {
                    "provider": "cluster-self-vpc",
                    "vpc_name": cluster.name,
                    "cidr_block": cluster.network.vpc,
                },
            ]
            for peering in cluster.peering.connections:
                if isinstance(peering, ClusterPeeringConnectionAccountVPCMeshV1):
                    aws_account_uid = peering.account.uid
                    settings = queries.get_secret_reader_settings()
                    accounts = queries.get_aws_accounts(uid=aws_account_uid)
                    awsapi = AWSApi(1, accounts, settings=settings, init_users=False)
                    mesh_results = awsapi.get_vpcs_details(
                        accounts[0], peering.tags or {}
                    )
                    for mesh_result in mesh_results:
                        vpc_peering_info = {
                            "provider": peering.provider,
                            "vpc_name": mesh_result["vpc_id"],
                            "cidr_block": mesh_result["cidr_block"],
                        }
                        peerings_entries.append(vpc_peering_info)
                if isinstance(peering, ClusterPeeringConnectionAccountV1):
                    cidr_block = str(peering.vpc.cidr_block)
                    vpc_peering_info = {
                        "provider": peering.provider,
                        "vpc_name": peering.vpc.name,
                        "cidr_block": cidr_block,
                    }
                    peerings_entries.append(vpc_peering_info)
                if isinstance(
                    peering,
                    ClusterPeeringConnectionClusterRequesterV1
                    | ClusterPeeringConnectionClusterAccepterV1,
                ):
                    assert peering.cluster.network
                    vpc_peering_info = {
                        "provider": peering.provider,
                        "vpc_name": peering.cluster.name,
                        "cidr_block": peering.cluster.network.vpc,
                    }
                    peerings_entries.append(vpc_peering_info)
            find_overlap = find_cidr_overlap(cluster.name, peerings_entries)
            if find_overlap:
                return False
    return True


def find_cidr_overlap(cluster_name: str, input_list: list) -> bool:
    for i in range(len(input_list)):
        compared_vpc = input_list[i]
        for j in range(i + 1, len(input_list)):
            comparing_vpc = input_list[j]
            if ipaddress.ip_network(compared_vpc["cidr_block"]).overlaps(
                ipaddress.ip_network(comparing_vpc["cidr_block"])
            ):
                logging.error(f"VPC peering error in cluster {cluster_name}")
                logging.error(
                    f"vpc [{compared_vpc['vpc_name']}] with cidr block [{compared_vpc['cidr_block']}] provided by [{compared_vpc['provider']}] overlaps with vpc [{comparing_vpc['vpc_name']}] with cidr block [{comparing_vpc['cidr_block']}] provided by [{comparing_vpc['provider']}]"
                )
                return True
    return False


def validate_no_internal_to_public_peerings(
    query_data: VpcPeeringsValidatorQueryData,
) -> bool:
    """Iterate over VPC peerings of internal clusters and validate the peer is not public."""
    valid = True
    found_pairs: list[set[str]] = []
    clusters: list[ClusterV1] = query_data.clusters or []
    for cluster in clusters:
        if not cluster.internal or not cluster.peering:
            continue
        for connection in cluster.peering.connections:
            if connection.provider not in {
                "cluster-vpc-accepter",
                "cluster-vpc-requester",
            }:
                continue
            connection = cast(
                "ClusterPeeringConnectionClusterAccepterV1 | ClusterPeeringConnectionClusterRequesterV1",
                connection,
            )
            peer = connection.cluster
            if peer.internal or (peer.spec and peer.spec.private):
                continue

            valid = False
            pair = {cluster.name, peer.name}
            if pair in found_pairs:
                continue
            found_pairs.append(pair)
            logging.error(
                f"found internal to public vpc peering: {cluster.name} <-> {peer.name}"
            )

    return valid


def validate_no_public_to_public_peerings(
    query_data: VpcPeeringsValidatorQueryData,
) -> bool:
    """Iterate over VPC peerings of public clusters and validate the peer is not public."""
    valid = True
    found_pairs: list[set[str]] = []
    clusters: list[ClusterV1] = query_data.clusters or []
    for cluster in clusters:
        if (
            cluster.internal
            or (cluster.spec and cluster.spec.private)
            or not cluster.peering
        ):
            continue
        for connection in cluster.peering.connections:
            if connection.provider not in {
                "cluster-vpc-accepter",
                "cluster-vpc-requester",
            }:
                continue
            connection = cast(
                "ClusterPeeringConnectionClusterAccepterV1 | ClusterPeeringConnectionClusterRequesterV1",
                connection,
            )
            peer = connection.cluster
            if peer.internal or (peer.spec and peer.spec.private):
                continue

            valid = False
            pair = {cluster.name, peer.name}
            if pair in found_pairs:
                continue
            found_pairs.append(pair)
            logging.error(
                f"found public to public vpc peering: {cluster.name} <-> {peer.name}"
            )

    return valid


def run(dry_run: bool) -> None:
    query_data = vpc_peerings_validator.query(query_func=gql.get_api().query)

    valid = True
    if not validate_no_internal_to_public_peerings(query_data):
        valid = False
    if not validate_no_public_to_public_peerings(query_data):
        valid = False
    if not validate_no_cidr_overlap(query_data):
        valid = False

    if not valid:
        sys.exit(ExitCodes.ERROR)
