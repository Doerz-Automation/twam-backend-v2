#!/bin/bash
# Nginx and Certbot initial setup script.
# This script will be run manually by the user once DNS is propagated.

DOMAIN_NAME=$1

if [ -z "$DOMAIN_NAME" ]; then
    echo "Please provide your domain name as an argument. e.g. ./nginx-setup.sh api.yourcompany.com"
    exit 1
fi

echo "Setting up Nginx configuration for $DOMAIN_NAME..."

mkdir -p nginx/conf.d
mkdir -p certbot/www
mkdir -p certbot/conf

cat << NGINX_CONF > nginx/conf.d/default.conf
server {
    listen 80;
    server_name $DOMAIN_NAME;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}
NGINX_CONF

echo "Basic HTTP config created. Running Certbot to generate the first certificate..."

# Ensure the containers are running (especially nginx for the .well-known challenge)
docker compose up -d nginx

docker compose run --rm certbot certonly --webroot -w /var/www/certbot -d $DOMAIN_NAME --email you@$DOMAIN_NAME --agree-tos --no-eff-email --force-renewal

echo "Certificates generated. Updating Nginx config to enable HTTPS..."

cat << HTTPS_CONF > nginx/conf.d/default.conf
server {
    listen 80;
    server_name $DOMAIN_NAME;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name $DOMAIN_NAME;

    ssl_certificate /etc/nginx/ssl/live/$DOMAIN_NAME/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/live/$DOMAIN_NAME/privkey.pem;

    location / {
        resolver 127.0.0.11 valid=30s;
        set \$upstream_backend backend;
        proxy_pass http://\$upstream_backend:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
HTTPS_CONF

echo "Restarting Nginx..."
docker compose restart nginx

echo "Setup complete! Visit https://$DOMAIN_NAME"
