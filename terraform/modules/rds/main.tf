# PostgreSQL with pgvector. One instance holds domain tables, LangGraph
# checkpoints, and embeddings — see ADR-005.

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
  tags       = merge(var.tags, { Name = var.name })
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.database_name
  username = var.master_username

  # NOT an injected password. RDS generates one, stores it in Secrets Manager,
  # and rotates it every 7 days with no Lambda to maintain.
  #
  # The decisive reason is a failure that only appears a week after deploy: AWS
  # states plainly that a secret injected into a task definition is NOT updated
  # when it rotates. Inject the password and the service works perfectly until
  # the first rotation, then cannot authenticate. Fetching from Secrets Manager
  # at connect time is the only version that survives.
  #
  # It also keeps the password out of Terraform state entirely.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]
  publicly_accessible    = false

  # Single-AZ is a deliberate cost decision for a demo (~$16/mo vs roughly
  # double). Production would be multi_az = true; the tradeoff is that a failure
  # here means downtime and a restore rather than a failover.
  multi_az = var.multi_az

  backup_retention_period = var.backup_retention_period
  skip_final_snapshot     = var.skip_final_snapshot
  deletion_protection     = var.deletion_protection
  apply_immediately       = true

  performance_insights_enabled    = false
  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = merge(var.tags, { Name = var.name })
}

# pgvector is NOT configured here, deliberately.
#
# It is a trusted extension: `CREATE EXTENSION vector;` needs no
# shared_preload_libraries entry, no parameter group change, no reboot and no
# superuser. There is also no Terraform resource for creating an extension. It
# belongs in migration 0001, which is where it lives.
