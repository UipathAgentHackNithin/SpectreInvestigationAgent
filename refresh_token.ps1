$BASE_URL = "https://staging.uipath.com/ad89db7f-af81-463f-865d-6c373f2feb96/ab8ad4cb-8820-42e7-a658-210ffaa23b75"
$PAT = "rt_7D4C42F51A1ECB3E5260BBB95AD4DFC32D865851D604F504B04E094991DC59BA-1"
$AUTH_PATH = "$PSScriptRoot\.uipath\.auth.json"
$ASSET_ID = 586682
$FOLDER_ID = 3087542

# Step 1: force fresh login
Write-Host "Authenticating..." -ForegroundColor Cyan
uipath auth --staging --base-url $BASE_URL --force

# Step 2: read new refresh token
$auth = Get-Content $AUTH_PATH | ConvertFrom-Json
$refreshToken = $auth.refresh_token
Write-Host "New refresh token: $($refreshToken.Substring(0, 50))..." -ForegroundColor Green

# Step 3: update asset in Orchestrator
$headers = @{
    "Authorization" = "Bearer $PAT"
    "Content-Type" = "application/json"
    "X-UIPATH-OrganizationUnitId" = "$FOLDER_ID"
}
$body = @{
    "Id" = $ASSET_ID
    "Name" = "SpectreRefreshToken"
    "ValueType" = "Text"
    "StringValue" = $refreshToken
} | ConvertTo-Json

$resp = Invoke-RestMethod -Uri "$BASE_URL/orchestrator_/odata/Assets($ASSET_ID)" -Method Put -Headers $headers -Body $body
Write-Host "Asset updated successfully!" -ForegroundColor Green
