# Phase 10: Domain + HTTPS

This phase gives the public API a stable HTTPS URL so GitHub Pages can call it
from the browser.

GitHub Pages is HTTPS. If the API is still `http://...`, the browser blocks the
request as mixed content.

## Dynu Role

Dynu is DNS here. AWS still needs the TLS certificate in ACM because the AWS
ALB terminates HTTPS.

Recommended DNS shape:

```text
api.<your-domain>  CNAME  <aws-alb-hostname>
```

If you only use a Dynu Dynamic DNS host with an IPv4 `A` record, that is not
ideal for EKS ALB because ALB IPs can change. Prefer a CNAME record to the ALB
hostname when Dynu allows it.

## What This Phase Adds

- ACM certificate request helper.
- ACM certificate status checker.
- HTTPS Ingress patch helper for AWS Load Balancer Controller.
- Dynu DNS instructions.

## Files

```text
scripts/deploy/request_acm_certificate.ps1
scripts/deploy/check_acm_certificate.ps1
scripts/deploy/apply_api_https_ingress.ps1
```

## Prerequisites

Complete these first:

- Phase 4: AWS infrastructure
- Phase 5: EKS add-ons with AWS Load Balancer Controller
- Phase 7: API on EKS

Tools:

```powershell
aws --version
kubectl version --client
```

## Step 1: Request ACM Certificate

Pick an API domain, for example:

```text
api.traffic-mlops.webredirect.org
```

Then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/request_acm_certificate.ps1 `
  -ApiDomainName "api.traffic-mlops.webredirect.org" `
  -AwsRegion "ap-southeast-1"
```

The script prints:

- `CertificateArn`
- DNS validation CNAME name
- DNS validation CNAME value

## Step 2: Add ACM Validation Record In Dynu

In Dynu, add the printed DNS validation record:

```text
Type : CNAME
Name : <printed ACM validation name>
Value: <printed ACM validation value>
```

Then check until ACM becomes `ISSUED`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/check_acm_certificate.ps1 `
  -CertificateArn "<certificate-arn>" `
  -AwsRegion "ap-southeast-1"
```

Do not continue until the certificate status is:

```text
ISSUED
```

## Step 3: Apply HTTPS To API Ingress

First confirm the EKS cluster name:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/list_eks_clusters.ps1 `
  -AwsRegion "ap-southeast-1"
```

If Phase 4 used the default values, the cluster name is usually:

```text
traffic-mlops-dev
```

Then patch the existing API Ingress:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/apply_api_https_ingress.ps1 `
  -ApiDomainName "api.traffic-mlops.webredirect.org" `
  -CertificateArn "<certificate-arn>" `
  -AwsRegion "ap-southeast-1" `
  -ClusterName "traffic-mlops-dev" `
  -FrontendOrigin "https://phuctt205dev.github.io"
```

This patches the existing API Ingress to:

- listen on HTTP 80 and HTTPS 443
- redirect HTTP to HTTPS
- attach the ACM certificate
- route only the selected API host
- update `CORS_ORIGINS`

The script prints the AWS ALB hostname.

## Step 4: Point Dynu Domain To ALB

In Dynu, add or update the API domain record:

```text
Type : CNAME
Host : api.traffic-mlops.webredirect.org
Value: <aws-alb-hostname>
```

Wait for DNS propagation.

Check:

```powershell
Resolve-DnsName api.traffic-mlops.webredirect.org
```

## Step 5: Smoke Test HTTPS API

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/smoke_api_eks.ps1 `
  -ApiBaseUrl "https://api.traffic-mlops.webredirect.org/api/v1" `
  -AdminEmail "admin@example.com" `
  -AdminPassword "<bootstrap-admin-password>"
```

## Step 6: Reconnect GitHub Pages

After HTTPS works, run Phase 9 again:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/deploy/connect_frontend_api.ps1 `
  -ApiBaseUrl "https://api.traffic-mlops.webredirect.org/api/v1"
```

Then wait for GitHub Actions to redeploy the frontend.

## Phase 10 Done Criteria

- ACM certificate status is `ISSUED`.
- Dynu validation CNAME exists.
- API domain CNAME points to the ALB hostname.
- `https://<api-domain>/api/v1/health/live` works.
- HTTP requests redirect to HTTPS.
- GitHub Pages can call the API without mixed-content errors.
