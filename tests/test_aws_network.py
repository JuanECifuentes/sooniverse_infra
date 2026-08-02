"""
Pruebas unitarias de scripts/aws_network.py usando moto (sin llamadas reales a AWS).
Cubre: idempotencia de provision(), tags, reglas SG->SG, orden de destroy,
guarda anti-VPC-default y negativa a borrar recursos ajenos.
"""

import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from aws_network import (  # noqa: E402
    TAG_DEPLOYMENT,
    TAG_MANAGED,
    AwsNetworkManager,
    DefaultVpcGuardError,
    NetworkSpec,
    compute_subnet_cidrs,
)
from infra_state import InMemoryInfraStateStore  # noqa: E402

REGION = "us-east-1"


def make_spec(**overrides):
    defaults = dict(
        client_id="acme",
        environment="prod",
        region=REGION,
        vpc_cidr="10.0.0.0/16",
        az_count=1,
        nat_mode="single",
        enable_s3_endpoint=True,
        admin_cidrs=["1.2.3.4/32"],
        public_cidrs=["0.0.0.0/0"],
        gateway_public_ports=[4000, 8000, 8080],
        worker_ports=[8007],
        expose_direct_ports=False,
    )
    defaults.update(overrides)
    return NetworkSpec(**defaults)


@pytest.fixture
def manager():
    with mock_aws():
        state = InMemoryInfraStateStore()
        spec = make_spec()
        mgr = AwsNetworkManager(spec, state=state, session=boto3.Session(region_name=REGION))
        yield mgr


def test_compute_subnet_cidrs_deterministic_and_non_overlapping():
    public, private = compute_subnet_cidrs("10.0.0.0/16", 2)
    assert len(public) == 2 and len(private) == 2
    public2, private2 = compute_subnet_cidrs("10.0.0.0/16", 2)
    assert public == public2 and private == private2
    import ipaddress

    nets = [ipaddress.ip_network(c) for c in public + private]
    for i, a in enumerate(nets):
        for b in nets[i + 1 :]:
            assert not a.overlaps(b)


def test_provision_creates_tagged_resources(manager):
    outputs = manager.provision()

    assert outputs.vpc_id.startswith("vpc-")
    assert len(outputs.public_subnet_ids) == 1
    assert len(outputs.private_subnet_ids) == 1
    assert outputs.nat_gateway_ids
    assert outputs.sg_gateway_id and outputs.sg_workers_id

    vpc = manager.ec2.describe_vpcs(VpcIds=[outputs.vpc_id])["Vpcs"][0]
    tags = {t["Key"]: t["Value"] for t in vpc["Tags"]}
    assert tags[TAG_MANAGED] == "true"
    assert tags[TAG_DEPLOYMENT] == manager.deployment_id
    assert tags["Name"] == "sooniverse-acme-prod-vpc"


def test_provision_is_idempotent(manager):
    first = manager.provision()
    second = manager.provision()

    assert first.vpc_id == second.vpc_id
    assert sorted(first.public_subnet_ids) == sorted(second.public_subnet_ids)
    assert sorted(first.nat_gateway_ids) == sorted(second.nat_gateway_ids)

    vpcs = manager.ec2.describe_vpcs(
        Filters=[{"Name": f"tag:{TAG_DEPLOYMENT}", "Values": [manager.deployment_id]}]
    )["Vpcs"]
    assert len(vpcs) == 1


def test_security_group_rules_use_sg_reference_not_cidr(manager):
    outputs = manager.provision()

    workers_sg = manager.ec2.describe_security_groups(GroupIds=[outputs.sg_workers_id])["SecurityGroups"][0]
    for perm in workers_sg["IpPermissions"]:
        assert not perm.get("IpRanges"), "El SG de workers no debe aceptar reglas por CIDR"
        assert perm.get("UserIdGroupPairs"), "El SG de workers debe referenciar otro SG (SG->SG)"

    source_group_ids = {
        pair["GroupId"] for perm in workers_sg["IpPermissions"] for pair in perm["UserIdGroupPairs"]
    }
    assert outputs.sg_gateway_id in source_group_ids


def test_gateway_sg_only_opens_admin_and_public_ports_by_default(manager):
    outputs = manager.provision()
    gw_sg = manager.ec2.describe_security_groups(GroupIds=[outputs.sg_gateway_id])["SecurityGroups"][0]
    ports = {perm["FromPort"] for perm in gw_sg["IpPermissions"]}
    assert ports == {22, 80}  # sin 4000/8000/8080 porque expose_direct_ports=False


def test_default_vpc_guard_blocks_operations():
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        default_vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]

        state = InMemoryInfraStateStore()
        spec = make_spec()
        mgr = AwsNetworkManager(spec, state=state, session=boto3.Session(region_name=REGION))

        with pytest.raises(DefaultVpcGuardError):
            mgr._guard_not_default_vpc(default_vpc["VpcId"])


def test_destroy_order_matches_creation_reverse(manager):
    manager.provision()
    plan = manager.plan_destroy()
    components_in_order = [item.component for item in plan]

    assert components_in_order.index("sg-workers") < components_in_order.index("vpc")
    assert components_in_order.index("nat") < components_in_order.index("vpc")
    assert components_in_order.index("subnet-public") < components_in_order.index("vpc")
    assert components_in_order.index("nat") < components_in_order.index("eip")


def test_destroy_dry_run_makes_no_mutating_calls(manager):
    outputs = manager.provision()
    report = manager.destroy(dry_run=True)

    assert not report.succeeded
    assert not report.failed
    vpc = manager.ec2.describe_vpcs(VpcIds=[outputs.vpc_id])["Vpcs"]
    assert vpc  # sigue existiendo


def test_destroy_full_cycle_removes_everything(manager):
    from botocore.exceptions import ClientError

    outputs = manager.provision()
    report = manager.destroy()

    assert report.ok, report.failed
    with pytest.raises(ClientError):
        manager.ec2.describe_vpcs(VpcIds=[outputs.vpc_id])


def test_destroy_refuses_resources_with_mismatched_tags(manager):
    outputs = manager.provision()

    # Simula que el sg-workers en AWS ya no lleva nuestro tag de deployment (p.ej.
    # fue recreado a mano) -> el destroy debe negarse a borrarlo.
    manager.ec2.create_tags(
        Resources=[outputs.sg_workers_id],
        Tags=[{"Key": TAG_DEPLOYMENT, "Value": "otro-deployment-ajeno"}],
    )

    report = manager.destroy()
    skipped_ids = {item.aws_id for item in report.skipped_not_ours}
    assert outputs.sg_workers_id in skipped_ids

    still_there = manager.ec2.describe_security_groups(GroupIds=[outputs.sg_workers_id])["SecurityGroups"]
    assert still_there


def test_destroy_refuses_resources_not_managed_by_us():
    with mock_aws():
        state = InMemoryInfraStateStore()
        spec = make_spec()
        mgr = AwsNetworkManager(spec, state=state, session=boto3.Session(region_name=REGION))
        outputs = mgr.provision()

        # Marca el recurso como ajeno (p.ej. adoptado, 'gestion_red: existente').
        state.record_resource(
            mgr.deployment_id,
            resource_type="vpc",
            component="vpc",
            aws_id=outputs.vpc_id,
            region=REGION,
            delete_order=80,
            managed_by_us=False,
            state="active",
        )

        report = mgr.destroy()
        vpc_items = [i for i in report.skipped_not_ours if i.aws_id == outputs.vpc_id]
        assert vpc_items
        assert mgr.ec2.describe_vpcs(VpcIds=[outputs.vpc_id])["Vpcs"]


def test_scan_orphans_detects_untracked_tagged_resources(manager):
    manager.provision()

    orphan_resp = manager.ec2.create_vpc(CidrBlock="10.99.0.0/16")
    orphan_vpc_id = orphan_resp["Vpc"]["VpcId"]
    manager.ec2.create_tags(
        Resources=[orphan_vpc_id],
        Tags=[{"Key": TAG_MANAGED, "Value": "true"}, {"Key": "Name", "Value": "huerfano"}],
    )

    orphans = manager.scan_orphans()
    orphan_ids = {o["aws_id"] for o in orphans}
    assert orphan_vpc_id in orphan_ids
