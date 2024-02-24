import os

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_lambda,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_efs as efs,
    aws_autoscaling as autoscaling,
)
from constructs import Construct

from .get_param import get_param

### Defaults, override with env vars
# DOCKER_IMAGE = "amazon/amazon-ecs-sample"
# DOCKER_IMAGE = "nginx:latest"
DOCKER_IMAGE = "itzg/minecraft-server"
# GAME_PORT = 80
GAME_PORT = 25565
INSTANCE_TYPE = "m5.large"

DATA_DIR = "/data"


class GameManagerStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, vpc, sg_ecs_traffic, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.docker_image = get_param(self, "DOCKER_IMAGE", default=DOCKER_IMAGE)
        self.game_port = get_param(self, "GAME_PORT", default=GAME_PORT)
        self.instance_type = get_param(self, "INSTANCE_TYPE", default=INSTANCE_TYPE)

        self.vpc = vpc
        self.sg_ecs_traffic = sg_ecs_traffic

        self.sg_ecs_traffic.connections.allow_from(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(self.game_port),  # <---- NOTE: TCP is hard-coded here too. Look into if we need to support UDP too.
            description="Game port to open traffic IN from",
        )

        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/Cluster.html
        self.ecs_cluster = ecs.Cluster(
            self,
            "ecs-cluster",
            cluster_name=f"{construct_id}-ecs-cluster",
            vpc=self.vpc,
        )

        ## Permissions for inside the container:
        self.ec2_role = iam.Role(
            self,
            "ec2-execution-role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="This instance's permissions, inside the container",
        )

        ## Let it register to a ecs cluster:
        # https://docs.aws.amazon.com/AmazonECS/latest/developerguide/security-iam-awsmanpol.html#instance-iam-role-permissions
        self.ec2_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEC2ContainerServiceforEC2Role"))

        ## For Running Commands (on container creation I think?)
        self.ec2_user_data = ec2.UserData.for_linux()
        # self.ec2_user_data.add_commands()

        ## Contains the configuration information to launch an instance, and stores launch parameters
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ec2/LaunchTemplate.html
        self.launch_template = ec2.LaunchTemplate(
            self,
            "ASG-LaunchTemplate",
            instance_type=ec2.InstanceType(self.instance_type),
            ## Needs to be an "EcsOptimized" image to register to the cluster
            machine_image=ecs.EcsOptimizedImage.amazon_linux2(),
            # machine_image=ecs.EcsOptimizedImage.amazon_linux(),
            # Lets Specific traffic to/from the instance:
            security_group=self.sg_ecs_traffic,
            user_data=self.ec2_user_data,
            role=self.ec2_role,
        )


        ## A Fleet represents a managed set of EC2 instances:
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_autoscaling/AutoScalingGroup.html
        self.auto_scaling_group = autoscaling.AutoScalingGroup(
            self,
            "ASG",
            vpc=self.vpc,
            launch_template=self.launch_template,
            desired_capacity=0,
            min_capacity=0,
            max_capacity=1,
            # IDK why I have to set this, the default is False. Maybe if you switch AMI's, the old AMI's
            # gain the protection? Test later and see if this needs to be set once the stack is more stable:
            new_instances_protected_from_scale_in=False,
        )

        ## This allows an ECS cluster to target a specific EC2 Auto Scaling Group for the placement of tasks.
        # Can ensure that instances are not prematurely terminated while there are still tasks running on them.
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/AsgCapacityProvider.html
        self.capacity_provider = ecs.AsgCapacityProvider(
            self,
            "AsgCapacityProvider",
            auto_scaling_group=self.auto_scaling_group,
            # machine_image_type=ecs.MachineImageType.AMAZON_LINUX_2,
            # Let me delete the stack!!:
            enable_managed_termination_protection=False,
        )
        self.ecs_cluster.add_asg_capacity_provider(self.capacity_provider)

        ## Persistent Storage:
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_efs.FileSystem.html
        self.efs_file_system = efs.FileSystem(
            self,
            "efs-file-system",
            vpc=self.vpc,
            # TODO: Just for developing. Keep users minecraft worlds SAFE!!
            # (note, what's the pros/cons of RemovalPolicy.RETAIN vs RemovalPolicy.SNAPSHOT?)
            removal_policy=RemovalPolicy.DESTROY,
        )

        ## Access the Persistent Storage:
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_efs/AccessPoint.html
        self.access_point = efs.AccessPoint(
            self,
            "efs-access-point",
            file_system=self.efs_file_system,
        )


        ## The details of a task definition run on an EC2 cluster.
        # (Root task definition, attach containers to this)
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/TaskDefinition.html
        self.task_definition = ecs.Ec2TaskDefinition(
            self,
            "task-definition",

            ## TODO: Says this can be locked down the most, and works with both windows/linux containers:
            ## BUT no way to assign it a public IP I can find. Compare with other MC Stack, see if they create
            ## A NAT or not. If they do, they're hella expensive though. (https://github.com/aws/aws-cdk/issues/13348)
            # network_mode=ecs.NetworkMode.AWS_VPC,
            # execution_role= ecs agent permissions (Permissions to pull images from ECR, BUT will automatically create one if not specified)
            # task_role= permissions for inside the container
        )
        self.volume_name = "efs-volume"
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/TaskDefinition.html#aws_cdk.aws_ecs.TaskDefinition.add_volume
        self.task_definition.add_volume(
            name=self.volume_name,
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=self.efs_file_system.file_system_id,
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=self.access_point.access_point_id,
                    # TODO: Look into enabling this:
                    # iam="ENABLED",
                ),
                transit_encryption="ENABLED",
            ),
        )

        # Allow the task to mount and Access EFS
        self.task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientRootAccess",
                    "elasticfilesystem:ClientWrite",
                    "elasticfilesystem:DescribeMountTargets",
                ],
                # resources=[f"arn:aws:elasticfilesystem:{self.region}:{self.account}:file-system/{self.efs_file_system.file_system_id}"],
                resources=[self.efs_file_system.file_system_arn],
            )
        )

        ## Details for add_container:
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/TaskDefinition.html#aws_cdk.aws_ecs.TaskDefinition.add_container
        ## And what it returns:
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/ContainerDefinition.html#aws_cdk.aws_ecs.ContainerDefinition
        self.container = self.task_definition.add_container(
            "game-container",
            image=ecs.ContainerImage.from_registry(self.docker_image),
            port_mappings=[
                ecs.PortMapping(host_port=self.game_port, container_port=self.game_port, protocol=ecs.Protocol.TCP),
            ],
            ## Hard limit. Won't ever go above this
            # memory_limit_mib=999999999,
            ## Soft limit. Container will go down to this if under heavy load, but can go higher
            memory_reservation_mib=4*1024,
        )
        self.container.add_mount_points(
            ecs.MountPoint(
                container_path=DATA_DIR,
                source_volume=self.volume_name,
                read_only=False,
            )
        )


        ## NO need for this I think? allow all outbound is the default anyways
        # ## Container Security Group, to allow EFS traffic
        # # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_ec2.SecurityGroup.html
        # self.container_sg = ec2.SecurityGroup(
        #     self,
        #     "container-sg",
        #     vpc=self.vpc,
        #     description="Security Group for the container, allows EFS traffic",
        # )
        # self.container_sg.connections.allow_to(
        #     self.efs_file_system.connections,
        #     port_range=ec2.Port.tcp(2049),
        #     description="Allow EFS traffic",
        # )

        ## This creates a service using the EC2 launch type on an ECS cluster
        # TODO: If you edit this in the console, there's a way to add "placement template - one per host" to it. Can't find the CDK equivalent rn.
        # https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/Ec2Service.html
        self.ec2_service = ecs.Ec2Service(
            self,
            "ec2-service",
            cluster=self.ecs_cluster,
            task_definition=self.task_definition,
            desired_count=0,
            circuit_breaker={
                # "enable": True, # Just having circuit breaker defined will enable it
                "rollback": False # Don't keep trying to restart the container if it fails
            },
            ## security_groups is only valid with AWS_VPC network mode:
            # security_groups=[self.sg_allow_game_traffic_in],
        )
        self.efs_file_system.connections.allow_default_port_from(self.ec2_service.connections)

        # Classic Lambda Function - for scaling up/down the container
        # https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_lambda.Function.html
        self.scale_container_lambda = aws_lambda.Function(
            self,
            "scale-container-lambda",
            description="Triggers the services and instance to scale up or down.",
            code=aws_lambda.Code.from_asset("./lambda-scale-container/"),
            handler="lambda_handler",
            runtime=aws_lambda.Runtime.PYTHON_3_12,
            timeout=Duration.seconds(300),
            # Lambda Functions in a public subnet can NOT access the internet.
            # This just acknowledges that.
            allow_public_subnet=True,
            environment={
                "ECS_CLUSTER_NAME": self.ecs_cluster.cluster_name,
                "ECS_CLUSTER_SERVICE": self.ec2_service.service_name,
                "ASG_NAME": self.auto_scaling_group.auto_scaling_group_name,
            },
            # Events to trigger this function. TODO:
            events=[],
            # Any permissions this function will need (PolicyStatement):
            initial_policy=[],
            # VPC Permissions, might need to scale up EC2:
            vpc=self.vpc,
            security_groups=[],
        )

        ## TODO: When getting to auto-scaling ec2 instance, this might help? Supposed to grab
        # the new IP address early:
        #   - https://github.com/aws/aws-cdk/blob/v1-main/packages/@aws-cdk-containers/ecs-service-extensions/lib/extensions/assign-public-ip/assign-public-ip.ts
        #   - https://stackoverflow.com/questions/68941663/is-there-anyway-to-determine-the-public-ip-of-a-fargate-container-before-it-beco
    
