# Deployment Instructions

## 1. Provision Infrastructure
Run Terraform commands from the `terraform/` directory:
```bash
cd terraform
terraform init
terraform apply
```
Note the output variable `instance_public_ip`.

## 2. GitHub Secrets
Configure the following secrets in your GitHub repository setting:
- `HOST`: The Elastic IP (`instance_public_ip`)
- `USERNAME`: `ubuntu`
- `SSH_PRIVATE_KEY`: The private SSH key used to access your EC2 instance.
- `DB_NAME`: Your PostgreSQL database name
- `DB_USER`: Your PostgreSQL user
- `DB_PASSWORD`: Your PostgreSQL password

## 3. Restoring Database
Once your containers are running, upload your backup file to the EC2 instance and restore it with:
```bash
docker exec -i twam-backend-db-1 psql -U twam_user -d twam_db < twam_backup.sql
```
*(Ensure to replace `twam-backend-db-1`, `twam_user` and `twam_db` if you named them differently).*
