# Container registries — one per service, since they are deployed independently.

resource "aws_ecr_repository" "this" {
  for_each = toset(var.repositories)

  name = "${var.name}/${each.value}"

  # IMMUTABLE is the important setting here, and it is what makes rollback work.
  # With mutable tags, a task definition pinned to `:v3` silently resolves to
  # whatever was pushed last — so rolling back to the previous revision can
  # redeploy the broken image. Immutable tags make a tag mean one artefact
  # forever.
  image_tag_mutability = "IMMUTABLE"

  # Catches a vulnerable base image at push time rather than after deploy.
  image_scanning_configuration {
    scan_on_push = true
  }

  # Dev only: lets `terraform destroy` clean up without emptying the repo first.
  # Production would leave this false so images cannot vanish under a running
  # service.
  force_delete = var.force_delete

  tags = var.tags
}

# Untagged layers accumulate on every rebuild and are pure cost. Tagged images
# are kept: they are what a rollback needs.
resource "aws_ecr_lifecycle_policy" "expire_untagged" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    }]
  })
}
