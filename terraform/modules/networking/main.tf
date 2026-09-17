# Network topology and the security-group chain.
#
# Three subnet tiers, because "private subnet" and "private communication" are
# different claims and the assignment asks for the second:
#
#   public        ALB only. The single internet-facing surface.
#   private_app   ECS tasks (frontend + backend). No inbound from the internet.
#   private_data  RDS. Reachable only from private_app.
#
# The security groups form a chain where each tier accepts traffic ONLY from the
# tier above it, referenced by security-group id rather than CIDR. That is the
# part worth saying out loud: a CIDR rule trusts an address range, which grows
# as you add subnets; an SG-to-SG rule trusts a specific set of tasks, and stays
# correct when the network changes underneath it.
#
#   internet -> alb_public -> frontend -> alb_internal -> backend -> rds
#
# Nothing can skip a link. The backend has no path from the internet at all,
# which is what makes the BFF proxy in ADR-001 a real boundary rather than a
# convention.

locals {
  # Two AZs: an ALB requires subnets in at least two, and RDS Multi-AZ would
  # need the same. Two is the minimum that is actually deployable.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  tags = merge(var.tags, { Module = "networking" })
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  # Both are required for private DNS to resolve inside the VPC — which is the
  # entire mechanism behind the private-domain requirement. Without them the
  # Route 53 private hosted zone silently fails to resolve and the frontend
  # cannot reach the backend by name.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.tags, { Name = "${var.name}-vpc" })
}

# ------------------------------------------------------------------- subnets

resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.tags, { Name = "${var.name}-public-${local.azs[count.index]}" })
}

resource "aws_subnet" "private_app" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = local.azs[count.index]

  tags = merge(local.tags, { Name = "${var.name}-app-${local.azs[count.index]}" })
}

resource "aws_subnet" "private_data" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 20)
  availability_zone = local.azs[count.index]

  tags = merge(local.tags, { Name = "${var.name}-data-${local.azs[count.index]}" })
}

# ------------------------------------------------------------------ routing

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${var.name}-igw" })
}

# ONE NAT gateway, not one per AZ.
#
# This is a deliberate cost decision, not an oversight: production would run one
# per AZ (~$66/mo) so a single AZ failure cannot cut egress for the whole VPC.
# At ~$33/mo it is the largest line item in this stack, and a demo environment
# that lives for days does not justify doubling it.
#
# The tradeoff is real and worth naming: if this AZ goes down, tasks in the other
# AZ lose outbound internet even though they are still running.
resource "aws_eip" "nat" {
  domain = "vpc" # `vpc = true` is deprecated
  tags   = merge(local.tags, { Name = "${var.name}-nat-eip" })
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]

  tags = merge(local.tags, { Name = "${var.name}-nat" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = merge(local.tags, { Name = "${var.name}-public-rt" })
}

# One private route table shared by both AZs, since there is one NAT gateway.
# With NAT per AZ this would need to be one table per AZ, each pointing at its
# local NAT — otherwise cross-AZ traffic is charged for every byte of egress.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = merge(local.tags, { Name = "${var.name}-private-rt" })
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private_app" {
  count          = length(aws_subnet.private_app)
  subnet_id      = aws_subnet.private_app[count.index].id
  route_table_id = aws_route_table.private.id
}

# The data tier has NO route to the NAT gateway. RDS does not need outbound
# internet, and not giving it any is cheaper than writing a rule to deny it.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${var.name}-data-rt" })
}

resource "aws_route_table_association" "private_data" {
  count          = length(aws_subnet.private_data)
  subnet_id      = aws_subnet.private_data[count.index].id
  route_table_id = aws_route_table.data.id
}

# S3 gateway endpoint. FREE, and the highest-value line in the whole stack:
# ECR serves image layers from S3, so without this every image pull is billed as
# NAT data processing at $0.045/GB. Interface endpoints (~$7.30/mo each per AZ)
# are a different question and lose to a single NAT at this scale — four of them
# across two AZs is ~$58/mo against ~$33. A gateway endpoint costs nothing.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = merge(local.tags, { Name = "${var.name}-s3-endpoint" })
}

# ---------------------------------------------------------- security groups

resource "aws_security_group" "alb_public" {
  name        = "${var.name}-alb-public"
  description = "Public ALB: the only internet-facing component"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.tags, { Name = "${var.name}-alb-public" })
}

resource "aws_security_group" "frontend" {
  name        = "${var.name}-frontend"
  description = "Next.js tasks: accept only from the public ALB"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.tags, { Name = "${var.name}-frontend" })
}

resource "aws_security_group" "alb_internal" {
  name        = "${var.name}-alb-internal"
  description = "Internal ALB: accept only from frontend tasks"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.tags, { Name = "${var.name}-alb-internal" })
}

resource "aws_security_group" "backend" {
  name        = "${var.name}-backend"
  description = "FastAPI tasks: accept only from the internal ALB. No public path."
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.tags, { Name = "${var.name}-backend" })
}

resource "aws_security_group" "rds" {
  name        = "${var.name}-rds"
  description = "Postgres: accept only from backend tasks"
  vpc_id      = aws_vpc.main.id
  tags        = merge(local.tags, { Name = "${var.name}-rds" })
}

# Rules are separate resources rather than inline blocks. Inline `ingress`/
# `egress` blocks take exclusive ownership of a group's rules, so mixing the two
# styles makes Terraform delete rules it did not create on every apply.

resource "aws_vpc_security_group_ingress_rule" "alb_public_https" {
  security_group_id = aws_security_group.alb_public.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "HTTPS from the internet"
}

resource "aws_vpc_security_group_ingress_rule" "alb_public_http" {
  security_group_id = aws_security_group.alb_public.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "HTTP, redirected to HTTPS at the listener"
}

resource "aws_vpc_security_group_ingress_rule" "frontend_from_alb" {
  security_group_id            = aws_security_group.frontend.id
  referenced_security_group_id = aws_security_group.alb_public.id
  from_port                    = var.frontend_port
  to_port                      = var.frontend_port
  ip_protocol                  = "tcp"
  description                  = "Only the public ALB may reach the frontend"
}

resource "aws_vpc_security_group_ingress_rule" "alb_internal_from_frontend" {
  security_group_id            = aws_security_group.alb_internal.id
  referenced_security_group_id = aws_security_group.frontend.id
  from_port                    = var.backend_port
  to_port                      = var.backend_port
  ip_protocol                  = "tcp"
  description                  = "Only frontend tasks may reach the internal ALB"
}

resource "aws_vpc_security_group_ingress_rule" "backend_from_alb_internal" {
  security_group_id            = aws_security_group.backend.id
  referenced_security_group_id = aws_security_group.alb_internal.id
  from_port                    = var.backend_port
  to_port                      = var.backend_port
  ip_protocol                  = "tcp"
  description                  = "Only the internal ALB may reach the backend"
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_backend" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.backend.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Only backend tasks may reach Postgres"
}

# Egress. Tasks need outbound for ECR pulls, Secrets Manager, CloudWatch, and
# Bedrock. RDS deliberately gets none — it has no route to the NAT anyway, so
# this is belt and braces.
resource "aws_vpc_security_group_egress_rule" "alb_public" {
  security_group_id = aws_security_group.alb_public.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "alb_internal" {
  security_group_id = aws_security_group.alb_internal.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "frontend" {
  security_group_id = aws_security_group.frontend.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "backend" {
  security_group_id = aws_security_group.backend.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
