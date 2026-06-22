# Phase 7: HTTPS With Dynu, Nginx And Certbot

This phase exposes the k3s Traffic API through a real HTTPS domain.

Target:

```text
GitHub Pages Frontend
        |
        v
https://api.traffic-mlops.webredirect.org
        |
        v
EC2 Node 1 public IP / Nginx / Certbot
        |
        v
http://127.0.0.1:30080
        |
        v
k3s traffic-api service
```

Airflow stays private on NodePort `30081` for admin testing only.

## Prerequisites

- Node 1 public Elastic IP: `54.206.56.204`
- Traffic API NodePort works from Node 1:

```bash
curl http://localhost:30080/api/v1/health/live
curl http://localhost:30080/api/v1/health/ready
```

- EC2 security group for Node 1 allows:

```text
80/tcp  from 0.0.0.0/0
443/tcp from 0.0.0.0/0
22/tcp  from your IP /32
```

## Step 1: Point Dynu DNS To Node 1

In Dynu, create or update this record:

```text
Type : A
Node : api
IPv4 : 54.206.56.204
TTL  : 120
```

The final hostname must resolve to:

```text
api.traffic-mlops.webredirect.org -> 54.206.56.204
```

Verify from Windows PowerShell:

```powershell
Resolve-DnsName api.traffic-mlops.webredirect.org
```

or from Node 1:

```bash
dig +short api.traffic-mlops.webredirect.org
```

## Step 2: Free Ports 80 And 443 If Traefik Uses Them

k3s installs Traefik by default. If Traefik already occupies host ports `80` or `443`, Nginx cannot bind them.

Check on Node 1:

```bash
sudo ss -tulpn | grep -E ':80|:443' || true
kubectl -n kube-system get pods,svc | grep -i traefik || true
```

If ports are already used by k3s Traefik, disable the bundled Traefik:

```bash
sudo mv /var/lib/rancher/k3s/server/manifests/traefik.yaml \
  /var/lib/rancher/k3s/server/manifests/traefik.yaml.disabled

kubectl -n kube-system delete helmchart traefik traefik-crd --ignore-not-found
kubectl -n kube-system delete svc traefik --ignore-not-found
kubectl -n kube-system delete deployment traefik --ignore-not-found
kubectl -n kube-system delete pod -l app.kubernetes.io/name=traefik --ignore-not-found
kubectl -n kube-system delete pod -l svccontroller.k3s.cattle.io/svcname=traefik --ignore-not-found
```

Check ports again:

```bash
sudo ss -tulpn | grep -E ':80|:443' || true
```

## Step 3: Install Nginx And Certbot On Node 1

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx dnsutils
```

## Step 4: Configure Nginx Reverse Proxy

Create the Nginx site:

```bash
sudo nano /etc/nginx/sites-available/traffic-api
```

Paste:

```nginx
server {
    listen 80;
    server_name api.traffic-mlops.webredirect.org;

    client_max_body_size 120m;

    location / {
        proxy_pass http://127.0.0.1:30080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 60s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
    }
}
```

Enable it:

```bash
sudo ln -sf /etc/nginx/sites-available/traffic-api /etc/nginx/sites-enabled/traffic-api
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Test HTTP:

```bash
curl http://api.traffic-mlops.webredirect.org/api/v1/health/live
```

Expected:

```json
{"status":"ok"}
```

## Step 5: Issue HTTPS Certificate

Run:

```bash
sudo certbot --nginx -d api.traffic-mlops.webredirect.org
```

When asked, choose the option to redirect HTTP to HTTPS.

Test:

```bash
curl https://api.traffic-mlops.webredirect.org/api/v1/health/live
curl https://api.traffic-mlops.webredirect.org/api/v1/health/ready
```

## Step 6: Persist API HTTPS Config In Kubernetes

Apply the k3s overlay again so the API config uses the HTTPS base URL:

```bash
cd ~/adaptive-mlops-traffic-forecast-self-research-NT114.Q21
git pull
kubectl kustomize k8s/overlays/k3s-core --load-restrictor LoadRestrictionsNone | kubectl apply -f -
kubectl rollout restart deployment/traffic-api -n traffic-mlops
kubectl rollout status deployment/traffic-api -n traffic-mlops
```

Verify:

```bash
kubectl get configmap traffic-config -n traffic-mlops -o yaml | grep VITE_API_BASE_URL
curl https://api.traffic-mlops.webredirect.org/api/v1/health/ready
```

## Step 7: Check Certificate Auto-Renewal

```bash
sudo certbot renew --dry-run
systemctl list-timers | grep -i certbot || true
```

## Optional: Close Temporary NodePort Access

After HTTPS works, direct public access to `30080` is no longer needed.

In the EC2 security group, remove or restrict:

```text
30080/tcp
```

Keep `30081/tcp` restricted to your IP only if you still want to access Airflow UI directly.
