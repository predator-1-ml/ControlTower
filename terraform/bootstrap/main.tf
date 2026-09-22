# Remote state backend. Run this ONCE, before anything in environments/.
#
# The chicken-and-egg: every other stack keeps its state in S3, but the bucket
# itself has to exist before a backend can point at it. So this one stack keeps
# its state locally and is committed to the repo. It is ~20 lines and changes
# roughly never, which is what makes that acceptable.
#
#   cd terraform/bootstrap && terraform init && terraform apply
#
# Note there is deliberately NO DynamoDB table. State locking now uses an S3
# lockfile (`use_lockfile = true` in the backend config); DynamoDB-based locking
# is deprecated and slated for removal. Most tutorials still show the table.

terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "Region for the state bucket. Must match `region` in environments/dev/backend.tf, or `terraform init` there cannot find the bucket."
  type        = string
  default     = "ap-southeast-1"
}

variable "state_bucket_name" {
  description = "Globally unique. S3 bucket names are a global namespace."
  type        = string
  default     = "control-tower-tfstate"
}

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name

  # State is the source of truth for everything the account contains. Losing it
  # is worse than losing the infrastructure, because Terraform then no longer
  # knows what it owns.
  lifecycle {
    prevent_destroy = true
  }
}

# Versioning is the actual recovery mechanism: a corrupted or truncated state
# write can be rolled back to the previous object version.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# State contains resource attributes in plaintext, which frequently includes
# things that should not be readable at rest.
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "state_bucket" {
  value       = aws_s3_bucket.state.id
  description = "Put this in environments/dev/backend.tf"
}
