# One Fargate service. Used twice — backend and frontend — because the
# assignment requires them to deploy independently.

resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  # Graviton: roughly 20% cheaper for identical work. Build natively on an arm64
  # runner rather than under QEMU emulation, which is slow enough to notice.
  runtime_platform {
    cpu_architecture        = var.cpu_architecture
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = var.name
      image     = var.image
      essential = true

      portMappings = [{
        containerPort = var.container_port
        protocol      = "tcp"
      }]

      environment = [
        for k, v in var.environment : { name = k, value = v }
      ]

      # No `secrets` block, deliberately. The only secret in this system is the
      # RDS password, and ECS resolves an injected secret ONCE, at task start: it
      # would work until the 7-day rotation and then fail to authenticate. The
      # app reads it from Secrets Manager per connection instead, using the TASK
      # role — see backend/app/db/checkpointer.py.

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = var.log_group_name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
          # non-blocking: a CloudWatch hiccup must not stall the application.
          # Without it the logging driver applies backpressure to the container.
          "mode"            = "non-blocking"
          "max-buffer-size" = "4m"
        }
      }

      healthCheck = {
        command     = var.health_check_command
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }

      # Caps at 120s on Fargate — the API rejects more. This is NOT the in-flight
      # budget: ECS waits out the target group's deregistration_delay BEFORE
      # sending SIGTERM, so that value (180s) is the real one. Long LangGraph runs
      # are protected by the checkpointer, not by shutdown timing.
      stopTimeout = 120
    }
  ])

  tags = var.tags
}

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.security_group_id]
    assign_public_ip = false # private subnets; egress goes via the NAT gateway
  }

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = var.name
    container_port   = var.container_port
  }

  # Rolls back automatically when a deployment cannot reach a steady state.
  # Without it a broken image sits there failing health checks until someone
  # notices. Both fields are required; enabling one alone does nothing.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Time for the app to boot before failed health checks count against it.
  health_check_grace_period_seconds = var.health_check_grace_period

  # Terraform owns everything with a lifecycle longer than one deploy. CI owns
  # exactly one mutable thing: which image digest is running. Without this,
  # the next `terraform apply` would revert whatever CI last deployed.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  tags = var.tags
}
