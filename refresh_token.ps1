$BASE_URL = "https://staging.uipath.com/ad89db7f-af81-463f-865d-6c373f2feb96/ab8ad4cb-8820-42e7-a658-210ffaa23b75"
$PAT = "rt_7D4C42F51A1ECB3E5260BBB95AD4DFC32D865851D604F504B04E094991DC59BA-1"
$AUTH_PATH = "$PSScriptRoot\.uipath\.auth.json"
$ENV_PATH = "$PSScriptRoot\.env"
$FOLDER_ID = 3087542

# Step 1: force fresh login
Write-Host "Authenticating..." -ForegroundColor Cyan
uipath auth --staging --base-url $BASE_URL --force

# Step 2: read new tokens from .auth.json
$auth = Get-Content $AUTH_PATH | ConvertFrom-Json
$refreshToken = $auth.refresh_token
$accessToken = $auth.access_token
Write-Host "New refresh token: $($refreshToken.Substring(0, 50))..." -ForegroundColor Green

# Step 3: update .env with new refresh token and access token
$envContent = [System.IO.File]::ReadAllLines($ENV_PATH)
$envContent = $envContent | Where-Object { $_ -notmatch '^UIPATH_REFRESH_TOKEN=' -and $_ -notmatch '^UIPATH_ACCESS_TOKEN=' }
$envContent += "UIPATH_REFRESH_TOKEN=$refreshToken"
$envContent += "UIPATH_ACCESS_TOKEN=$accessToken"
[System.IO.File]::WriteAllLines($ENV_PATH, $envContent)
Write-Host ".env updated with new refresh token and access token" -ForegroundColor Green

# Step 4: update Orchestrator asset
$headers = @{
    "Authorization" = "Bearer $PAT"
    "Content-Type" = "application/json"
    "X-UIPATH-OrganizationUnitId" = "$FOLDER_ID"
}
$lookup = Invoke-RestMethod -Uri "$BASE_URL/orchestrator_/odata/Assets?`$filter=Name eq 'SPECTRE_REFRESH_TOKEN'" -Method Get -Headers $headers
$ASSET_ID = $lookup.value[0].Id
if (-not $ASSET_ID) { Write-Error "SPECTRE_REFRESH_TOKEN asset not found"; exit 1 }

$body = @{
    "Id" = $ASSET_ID
    "Name" = "SPECTRE_REFRESH_TOKEN"
    "ValueType" = "Credential"
    "CredentialUsername" = "spectre"
    "CredentialPassword" = $refreshToken
    "AllowDirectApiAccess" = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri "$BASE_URL/orchestrator_/odata/Assets($ASSET_ID)" -Method Put -Headers $headers -Body $body
Write-Host "Orchestrator asset updated successfully!" -ForegroundColor Green
