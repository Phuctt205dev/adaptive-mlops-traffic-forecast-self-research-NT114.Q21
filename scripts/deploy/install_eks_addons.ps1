param(
    [string]$AwsRegion = "ap-southeast-1",

    [string]$ClusterName = "traffic-mlops-dev",

    [Parameter(Mandatory = $true)]
    [string]$VpcId,

    [Parameter(Mandatory = $true)]
    [string]$LoadBalancerControllerRoleArn
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$StorageClassManifest = Join-Path $RepositoryRoot "k8s\addons\storageclass-gp3.yaml"
$LoadBalancerRoleAnnotation = "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=$LoadBalancerControllerRoleArn"

Write-Host "Updating kubeconfig for $ClusterName"
aws eks update-kubeconfig --region $AwsRegion --name $ClusterName

Write-Host "Installing gp3 StorageClass"
kubectl apply -f $StorageClassManifest

Write-Host "Installing Metrics Server"
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ | Out-Null
helm repo update metrics-server | Out-Null
helm upgrade --install metrics-server metrics-server/metrics-server `
    --namespace kube-system `
    --set "args[0]=--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname"

Write-Host "Installing AWS Load Balancer Controller"
helm repo add eks https://aws.github.io/eks-charts | Out-Null
helm repo update eks | Out-Null
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller `
    --namespace kube-system `
    --set clusterName=$ClusterName `
    --set region=$AwsRegion `
    --set vpcId=$VpcId `
    --set serviceAccount.create=true `
    --set serviceAccount.name=aws-load-balancer-controller `
    --set $LoadBalancerRoleAnnotation

Write-Host "Waiting for add-on deployments"
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s
kubectl -n kube-system rollout status deployment/aws-load-balancer-controller --timeout=180s

Write-Host "EKS add-ons installed."
