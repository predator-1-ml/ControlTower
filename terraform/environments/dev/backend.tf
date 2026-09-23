terraform {
  required_version = ">= 1.11"

  backend "s3" {
    bucket = "control-tower-tfstate-187880375508"
    key    = "dev/terraform.tfstate"
    region = "ap-southeast-1"

    encrypt = true
    # S3 native locking. DynamoDB-based locking is deprecated and slated for
    # removal; most tutorials still show a lock table that is no longer needed.
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  # No `profile` here, deliberately. Locally it comes from AWS_PROFILE; in
  # GitHub Actions authentication is OIDC and there is no profile at all.
  # Hardcoding one would break CI.

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
