# One module, two uses: the public ALB in front of the frontend, and the internal
# ALB that gives the backend a stable private address.
#
# The internal one is the mechanism behind the assignment's "communicate
# privately using a domain name" requirement. Route 53 aliases a private hosted
# zone record at it (see environments/dev), so the frontend resolves a real DNS
# name rather than a task IP — and unlike Cloud Map, that name survives task
# replacement, which the crash-recovery demo depends on.

resource "aws_lb" "this" {
  name               = var.name
  internal           = var.internal
  load_balancer_type = "application"
  subnets            = var.subnet_ids
  security_groups    = [var.security_group_id]

  # 300s, well above the default 60. An SSE stream is one long-lived request, and
  # any byte in either direction resets this — so the backend's 15s heartbeat
  # alone keeps it open.
  #
  # Note the ceiling above it: client_keep_alive (default 3600s) is a hard
  # wall-clock cap that does NOT reset with traffic. That, not idle_timeout, is
  # the real maximum life of a single stream.
  idle_timeout = var.idle_timeout

  # Dev convenience; production would leave this on.
  enable_deletion_protection = false

  tags = merge(var.tags, { Name = var.name })
}

resource "aws_lb_target_group" "this" {
  name        = var.name
  port        = var.target_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip" # required for Fargate awsvpc networking

  # THE number that matters for in-flight work. ECS deregisters a target, waits
  # out this delay, and only THEN sends SIGTERM — so this, not stopTimeout, is
  # the real budget for finishing existing requests. stopTimeout caps at 120s on
  # Fargate, which is why the graph checkpoints instead of relying on it.
  deregistration_delay = var.deregistration_delay

  health_check {
    path                = var.health_check_path
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }

  # round_robin, NOT least_outstanding_requests. An open SSE stream counts as an
  # outstanding request for its entire life, so LOR would route almost all new
  # traffic to whichever task has fewest streams and starve the rest.
  load_balancing_algorithm_type = "round_robin"

  tags = var.tags
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

# HTTPS is configured only when a certificate is supplied. A take-home demo has
# no domain to validate a certificate against, so the public ALB runs HTTP and
# the docs say so plainly. The block exists to show where TLS terminates.
resource "aws_lb_listener" "https" {
  count = var.certificate_arn == null ? 0 : 1

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}
