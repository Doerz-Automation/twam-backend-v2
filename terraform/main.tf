# VPC
resource "aws_vpc" "twam_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "twam-vpc"
  }
}

# Public Subnet
resource "aws_subnet" "twam_public_subnet" {
  vpc_id                  = aws_vpc.twam_vpc.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = "${var.aws_region}a"

  tags = {
    Name = "twam-public-subnet"
  }
}

# Internet Gateway
resource "aws_internet_gateway" "twam_igw" {
  vpc_id = aws_vpc.twam_vpc.id

  tags = {
    Name = "twam-igw"
  }
}

# Route Table
resource "aws_route_table" "twam_public_rt" {
  vpc_id = aws_vpc.twam_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.twam_igw.id
  }

  tags = {
    Name = "twam-public-rt"
  }
}

# Route Table Association
resource "aws_route_table_association" "twam_public_rta" {
  subnet_id      = aws_subnet.twam_public_subnet.id
  route_table_id = aws_route_table.twam_public_rt.id
}

# Security Group
resource "aws_security_group" "twam_sg" {
  name        = "twam-sg"
  description = "Allow SSH, HTTP, and Django API inbound traffic"
  vpc_id      = aws_vpc.twam_vpc.id

  ingress {
    description = "SSH from anywhere"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Django API from anywhere"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS from anywhere"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "twam-sg"
  }
}

# Get latest Ubuntu 22.04 AMI
data "aws_ami" "ubuntu" {
  most_recent = true

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  owners = ["099720109477"] # Canonical
}

# EC2 Instance Key Pair
resource "aws_key_pair" "twam_deploy_key" {
  key_name   = "twam-github-deploy-key"
  public_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQC+YXpezzJM2abYWRVuYlVlNN/Rnl05YNznhuAJzScC5r8rb+SbqaQazH+/lm4WS6bLBlHthOURPKiS7jhcn2RGxlrduCUzZbT2/bGP7zABzu4Wt11h+J2LPHtEGOGLUe+82qiYuX9D1nzdUCjxP0Zpg1YW3i2lxxNa0R+iNO46ZjNLHqwzHKOdzeW3gwpTfOmS64NTQdYbgNpvDRqK4D+6iWIAH3B/7QQT9J/Nz+U+t8tHpDJCeMuwzyCnr5qa+h2PmMsGljAMTQIi7nAabYdsUslKssaUgH9qqj90+Iotj9+SGUBbqjgVD4+hg+6rFW7afLb+FxrjEfv4ve6lKO81H31AKHKSfIMXfWAfdj/m0Njs+pwLa/Zh0PQZuFrHy/oig9bSoBTo1ptvTKDwhD1ogn12r7IlGLvb6B5iLoo8Tk64HAZidFoERhVwFlpAqmeSZ6djDjHspA4t9ux4sfkE8/y9T2jTSPsxb3JxZLWKTpo+k7FntxZRFXf4rHSa/TYE+6s3N4NuKUCrC1eU9wOp4/QFGLWOi9cClokcAFLZJa6H1PMzPNCsGfDj4y0Jx7DyHoT/qOUQv7bjoTPqQM1Ni2ddRB5lJjxRg9sXu2iNrbddWd+ExjMEkEJdM1UIpXHFMlefrQGIV2c1lZbhx5hhcPTyJo+znKgfvrSbEt9pPQ== github-actions-deploy-key"
}

# EC2 Instance
resource "aws_instance" "twam_backend" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.instance_type
  subnet_id     = aws_subnet.twam_public_subnet.id
  key_name      = aws_key_pair.twam_deploy_key.key_name

  vpc_security_group_ids = [aws_security_group.twam_sg.id]

  # User data script to install Docker and Docker Compose
  user_data = <<-EOF
              #!/bin/bash
              apt-get update
              apt-get install -y ca-certificates curl gnupg
              install -m 0755 -d /etc/apt/keyrings
              curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
              chmod a+r /etc/apt/keyrings/docker.gpg

              echo \
                "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
                \"$(. /etc/os-release && echo "$VERSION_CODENAME")\" stable" | \
                tee /etc/apt/sources.list.d/docker.list > /dev/null

              apt-get update
              apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

              usermod -aG docker ubuntu
              systemctl enable docker
              systemctl start docker
              EOF

  tags = {
    Name = "twam-backend-instance"
  }
}

# Elastic IP
resource "aws_eip" "twam_eip" {
  instance = aws_instance.twam_backend.id
  domain   = "vpc"

  tags = {
    Name = "twam-eip"
  }
}
