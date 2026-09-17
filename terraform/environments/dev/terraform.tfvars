# Placeholder images so `terraform plan` runs before CI has pushed anything.
# Replace with real digests, or let the deploy workflow render the task
# definition. A digest beats a tag: a task definition storing :latest cannot be
# rolled back, because revision N-1 re-resolves to whatever :latest points at now.
backend_image  = "public.ecr.aws/docker/library/busybox:latest"
frontend_image = "public.ecr.aws/docker/library/busybox:latest"
