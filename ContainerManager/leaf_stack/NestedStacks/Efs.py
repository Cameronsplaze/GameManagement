
from aws_cdk import (
    NestedStack,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
)
from constructs import Construct



### Nested Stack info:
# https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.NestedStack.html
class Efs(NestedStack):
    def __init__(
        self,
        scope: Construct,
        leaf_construct_id: str,
        vpc: ec2.Vpc,
        task_definition: ecs.Ec2TaskDefinition,
        container: ecs.ContainerDefinition,
        volumes_config: list,
        volume_info_config: dict,
        sg_efs_traffic: ec2.SecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, "EfsNestedStack", **kwargs)

        ## Persistent Storage:
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_efs.FileSystem.html
        # TODO: When writing readme, add note on using DataSync to get info from old EFS to new one.
        #       (Just use console. IaC shouldn't try to copy data in. It will on every re-deploy...)
        efs_removal_policy = volume_info_config.get("RemovalPolicy", "RETAIN").upper()
        self.efs_file_system = efs.FileSystem(
            self,
            "efs-file-system",
            vpc=vpc,
            removal_policy=getattr(RemovalPolicy, efs_removal_policy),
            security_group=sg_efs_traffic,
            allow_anonymous_access=False,
        )


        ## Tell the EFS side that the task can access it:
        self.efs_file_system.grant_read_write(task_definition.task_role)
        ## (NOTE: There's another grant_root_access in EcsAsg.py ec2-role.
        #         I just didn't see a way to move it here without moving the role.)

        ### Settings for ALL access points:
        ## Create ACL:
        # (From the docs, if the `path` above does not exist, you must specify this)
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_efs.AccessPointOptions.html#createacl
        ap_acl = efs.Acl(owner_gid="1001", owner_uid="1001", permissions="700")

        ### Create a access point for the host:
        ## Creating an access point:
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_efs.FileSystem.html#addwbraccesswbrpointid-accesspointoptions
        ## What it returns:
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_efs.AccessPoint.html
        self.host_access_point = self.efs_file_system.add_access_point("efs-access-point-host", create_acl=ap_acl, path="/")

        ### Create mounts and attach them to the container:
        for volume_info in volumes_config:
            volume_path = volume_info["Path"]
            read_only = volume_info.get("ReadOnly", False)
            ## Create a unique name, take out non-alpha characters from the path:
            #   (Will be something like: `Minecraft-data`)
            volume_id = f"{container.container_name}-{''.join(filter(str.isalnum, volume_path))}"
            # Another access point, for the container (each volume gets it's own):
            access_point = self.efs_file_system.add_access_point(f"efs-access-point-{volume_id}", create_acl=ap_acl, path=volume_path)
            volume_name = f"efs-volume-{volume_id}"
            # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ecs.TaskDefinition.html#aws_cdk.aws_ecs.TaskDefinition.add_volume
            task_definition.add_volume(
                name=volume_name,
                # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ecs.EfsVolumeConfiguration.html
                efs_volume_configuration=ecs.EfsVolumeConfiguration(
                    file_system_id=self.efs_file_system.file_system_id,
                    # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ecs.AuthorizationConfig.html
                    authorization_config=ecs.AuthorizationConfig(
                        access_point_id=access_point.access_point_id,
                        iam="ENABLED",
                    ),
                    transit_encryption="ENABLED",
                ),
            )
            container.add_mount_points(
                ecs.MountPoint(
                    container_path=volume_path,
                    source_volume=volume_name,
                    read_only=read_only,
                )
            )
