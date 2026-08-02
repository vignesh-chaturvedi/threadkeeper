# VPC, two public subnets, one internet gateway. No NAT.
#
# Fargate tasks sit in public subnets with `assign_public_ip = true` and a
# security group that allows nothing inbound except the ALB. That is the same
# reachability a NAT gateway would give them, for $32/month less. The honest
# statement of the tradeoff: a task with a public IP is one misconfigured
# security group away from being reachable, where a private subnet would need a
# misconfigured security group *and* a route. For a demo holding no customer
# data that is the right side of the line; for the real thing it is not.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --------------------------------------------------------------------- groups
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public HTTP into the load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "Fargate tasks. Inbound only from the ALB."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App port, load balancer only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Outbound is open because the tasks call the Gemini API, ECR, Secrets Manager
  # and CloudWatch. Narrowing this needs VPC endpoints for the AWS services and
  # an egress proxy for the model — worth doing, out of scope here, and said out
  # loud rather than left as an unexplained 0.0.0.0/0.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "data" {
  name        = "${local.name}-data"
  description = "RDS and ElastiCache. Reachable only from the tasks."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  ingress {
    description     = "Redis"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

# ------------------------------------------------------------------------ alb
resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  # Long enough for the in-flight HTTP requests the ALB knows about. The turns
  # themselves run in background tasks the ALB cannot see, which is what
  # TK_DRAIN_TIMEOUT_S covers.
  deregistration_delay = local.deregistration_delay_s

  health_check {
    path = "/health/ready"
    # Readiness, not liveness: the question the load balancer is asking is
    # "should this target get traffic", and a draining task answers no. A
    # liveness check here would keep sending requests to a container on its way
    # out. They are different questions and this is the seam where it shows.
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # HTTP only. TLS needs a certificate, which needs a domain, which is a thing
  # to own rather than a thing to terraform. WhatsApp requires HTTPS for a real
  # webhook, so this is a demo endpoint and the README says so.
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
