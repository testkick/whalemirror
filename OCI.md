# Self-hosting on Oracle Cloud (alternative to Railway)

Always Free tier `VM.Standard.A1.Flex` (1 OCPU / 6 GB) is plenty. Ubuntu 22.04+.

1. Compute → Instances → Create (A1 shape, Ubuntu, your SSH key).
2. VCN Security List: ingress TCP 80 + 443 from 0.0.0.0/0. Do NOT open 8000.
3. On the instance:

    sudo apt update && sudo apt install -y docker.io docker-compose-v2
    sudo usermod -aG docker $USER && newgrp docker
    sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
    sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
    sudo netfilter-persistent save

4. Clone the repo, `cp .env.example .env`, set APP_PASSWORD / APP_SECRET /
   DOMAIN (A record → instance public IP), then:

    docker compose up -d --build

Caddy provisions TLS automatically. No domain? Leave DOMAIN=localhost, skip the
firewall rules, and use an SSH tunnel:

    ssh -L 8000:127.0.0.1:8000 ubuntu@<ip>   # browse http://localhost:8000
    # set COOKIE_SECURE=false in .env for tunnel (plain-HTTP) access
