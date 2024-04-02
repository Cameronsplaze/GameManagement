
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_route53 as route53,
    aws_iam as iam,
    aws_logs as logs,
    aws_lambda as aws_lambda,
)
from constructs import Construct

from .base_stack import ContainerManagerBaseStack


class DomainStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, container_name_id: str, base_stack: ContainerManagerBaseStack, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        ## The instance isn't up, use the "unknown" ip address:
        # https://www.lifewire.com/four-zero-ip-address-818384
        self.unavailable_ip = "0.0.0.0"
        # Never set TTL to 0, it's not defined in the standard
        self.unavailable_ttl = 1
        self.sub_domain_name = f"{container_name_id}.{base_stack.root_hosted_zone.zone_name}".lower()

        ## Log group for the Route53 DNS logs:
        self.route53_query_log_group = logs.LogGroup(
            self,
            f"{construct_id}-route53-query-log-group",
            log_group_name=f"/aws/route53/{construct_id}-query-logs",
            # Only need logs to trigger the lambda, don't need long-term:
            retention=logs.RetentionDays.ONE_DAY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        ## You can't grant direct access after creating the sub_hosted_zone, since it needs to
        # write to the log group on creation. AND you can't do a wildcard arn, since the
        # account number isn't in the arn.
        self.route53_query_log_group.grant_write(iam.ServicePrincipal("route53.amazonaws.com"))

        ## The subdomain for the Hosted Zone:
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_route53.PublicHostedZone.html
        self.sub_hosted_zone = route53.PublicHostedZone(
            self,
            f"{construct_id}-sub-hosted-zone",
            zone_name=self.sub_domain_name,
            query_logs_log_group_arn=self.route53_query_log_group.log_group_arn,
            comment=f"Hosted zone for {construct_id}: {self.sub_domain_name}",
        )
        self.sub_hosted_zone.apply_removal_policy(RemovalPolicy.DESTROY)
        # self.route53_query_log_group.grant_write(iam.ArnPrincipal(self.sub_hosted_zone.hosted_zone_arn))

        ## Tie the two hosted zones together:
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_route53.NsRecord.html
        self.ns_record = route53.NsRecord(
            self,
            f"{construct_id}-NsRecord",
            zone=base_stack.root_hosted_zone,
            values=self.sub_hosted_zone.hosted_zone_name_servers,
            record_name=self.sub_domain_name,
        )
        ## Add a record set that uses the base hosted zone
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_route53.RecordSet.html
        self.dns_record = route53.RecordSet(
            self,
            f"{construct_id}-DnsRecord",
            zone=self.sub_hosted_zone,
            record_name=self.sub_domain_name,
            record_type=route53.RecordType.A,
            target=route53.RecordTarget.from_values(self.unavailable_ip),
            ttl=Duration.seconds(self.unavailable_ttl),
        )