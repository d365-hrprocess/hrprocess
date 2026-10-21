#!/usr/bin/env bash
# Run using an existing approved Azure CLI login. This does not change Azure.
set -euo pipefail
umask 077
output="${1:-finance-demo-baseline}"
mkdir -p "$output"
subscription='60730d88-4cfc-4e6d-8c19-3e9591bc14bd'
group='ubti-hrprocess-rg'
webapp='ubtihrprocess'
az webapp show --subscription "$subscription" -g "$group" -n "$webapp" \
  --query '{id:id,name:name,defaultHostName:defaultHostName,state:state,httpsOnly:httpsOnly,enabledHostNames:enabledHostNames}' \
  -o json > "$output/site.json"
az webapp config show --subscription "$subscription" -g "$group" -n "$webapp" \
  --query '{linuxFxVersion:linuxFxVersion,appCommandLine:appCommandLine,healthCheckPath:healthCheckPath,alwaysOn:alwaysOn,http20Enabled:http20Enabled,webSocketsEnabled:webSocketsEnabled,ipSecurityRestrictions:ipSecurityRestrictions,ipSecurityRestrictionsDefaultAction:ipSecurityRestrictionsDefaultAction}' \
  -o json > "$output/runtime.json"
az webapp sitecontainers list --subscription "$subscription" -g "$group" -n "$webapp" \
  --query '[].{name:name,image:properties.image,targetPort:properties.targetPort,isMain:properties.isMain,startUpCommand:properties.startUpCommand,authType:properties.authType,userManagedIdentityClientId:properties.userManagedIdentityClientId,volumeMounts:properties.volumeMounts}' \
  -o json > "$output/containers.json"
az webapp deployment slot list --subscription "$subscription" -g "$group" -n "$webapp" \
  --query "[].{name:name,id:id,state:state}" -o json > "$output/slots.json"
az webapp auth show --subscription "$subscription" -g "$group" -n "$webapp" \
  --query '{enabled:platform.enabled,requireAuthentication:globalValidation.requireAuthentication,unauthenticatedClientAction:globalValidation.unauthenticatedClientAction,excludedPaths:globalValidation.excludedPaths,allowedAudiences:identityProviders.azureActiveDirectory.validation.allowedAudiences}' \
  -o json > "$output/authentication.json"
az webapp config appsettings list --subscription "$subscription" -g "$group" -n "$webapp" \
  --query "[?name=='WEBSITES_ENABLE_APP_SERVICE_STORAGE'].{name:name,value:value}" \
  -o json > "$output/storage-setting.json"
for route in healthz api/health hr ''; do
  filename="${route//\//-}"
  filename="${filename:-home}"
  curl --silent --show-error --max-time 20 --dump-header "$output/$filename.headers" \
    "https://ubtihrprocess-egg3f7awbsbjdjd5.centralindia-01.azurewebsites.net/$route" \
    --output "$output/$filename.body"
done
printf 'Saved private baseline to %s. Review Easy Auth and ingress before deployment.\n' "$output"
