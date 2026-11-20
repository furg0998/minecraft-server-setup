#Requires -Version 5.1
<#
.SYNOPSIS
  Deploys the Minecraft Bedrock on-demand server stack.

.EXAMPLE
  .\deploy.ps1 -AllowedPlayers "Steve:2533274790567123,Alex:2535412345678901" -PanelPassword "s3cret!"

.EXAMPLE
  # Minimal - generates a random panel password into Secrets Manager:
  .\deploy.ps1 -AllowedPlayers "Steve,Alex"
#>
[CmdletBinding()]
param(
  [string]$StackName       = 'minecraft-bedrock',
  [string]$Region          = '',
  [string]$ArtifactsBucket = '',
  [string]$PanelPassword   = '',
  [string]$AllowedPlayers  = '',
  [string]$ServerName      = 'Family Minecraft Server',
  [string]$GameMode        = 'survival',
  [string]$Difficulty      = 'normal',
  [int]$MaxPlayers         = 10,
  [string]$EnableAutoStop  = 'true',
  [int]$IdleShutdownMinutes = 45,
  [string]$EnableAllowList = 'true',
  [string]$EnableMap       = 'true'
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $Region) {
  try { $Region = aws configure get region 2>$null } catch { $Region = '' }
}
if (-not $Region) { $Region = 'us-east-1' }

try {
  $account = aws sts get-caller-identity --query Account --output text 2>$null
} catch { $account = '' }
if (-not $account -or $LASTEXITCODE -ne 0) {
  throw "AWS credentials not configured or expired. Run 'aws login' or 'aws configure' first."
}

# S3 bucket used by `aws cloudformation package` to stage the Lambda zip.
if (-not $ArtifactsBucket) { $ArtifactsBucket = "mc-bedrock-artifacts-$account-$Region" }
$bucketFound = aws s3api list-buckets `
  --query "Buckets[?Name=='$ArtifactsBucket'].Name" --output text
if (-not $bucketFound) {
  Write-Host "Creating artifacts bucket $ArtifactsBucket ..."
  if ($Region -eq 'us-east-1') {
    aws s3api create-bucket --bucket $ArtifactsBucket --region $Region | Out-Null
  } else {
    aws s3api create-bucket --bucket $ArtifactsBucket --region $Region `
      --create-bucket-configuration LocationConstraint=$Region | Out-Null
  }
}

$packaged = Join-Path $PSScriptRoot '.packaged.yaml'
Write-Host "Packaging template..."
aws cloudformation package `
  --template-file template.yaml `
  --s3-bucket $ArtifactsBucket `
  --output-template-file $packaged `
  --region $Region
if ($LASTEXITCODE -ne 0) { throw "cloudformation package failed" }

$overrides = @(
  "ServerName=$ServerName",
  "PanelPassword=$PanelPassword",
  "AllowedPlayers=$AllowedPlayers",
  "GameMode=$GameMode",
  "Difficulty=$Difficulty",
  "MaxPlayers=$MaxPlayers",
  "EnableAutoStop=$EnableAutoStop",
  "IdleShutdownMinutes=$IdleShutdownMinutes",
  "EnableAllowList=$EnableAllowList",
  "EnableMap=$EnableMap"
)

Write-Host "Deploying stack $StackName to $Region ..."
aws cloudformation deploy `
  --template-file $packaged `
  --stack-name $StackName `
  --capabilities CAPABILITY_IAM `
  --parameter-overrides @overrides `
  --region $Region
if ($LASTEXITCODE -ne 0) { throw "cloudformation deploy failed" }

Write-Host "`n=== Stack outputs ==="
aws cloudformation describe-stacks --stack-name $StackName --region $Region `
  --query 'Stacks[0].Outputs' --output table

Write-Host "`nControl panel URL:"
aws cloudformation describe-stacks --stack-name $StackName --region $Region `
  --query "Stacks[0].Outputs[?OutputKey=='PanelUrl'].OutputValue" --output text

if (-not $PanelPassword) {
  Write-Host "`nNo -PanelPassword was given; a random one was generated. Retrieve it with:"
  Write-Host "  aws secretsmanager get-secret-value --secret-id <PanelSecret from outputs> --query SecretString --output text --region $Region"
}
