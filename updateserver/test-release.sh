#!/bin/bash

updateFileName="TrueNAS-SCALE-${TRUENAS_VERSION}"
train="${TRUENAS_TRAIN}"
CODE_NAME=$(yq -r '.code_name' conf/build.manifest)
R2_ENDPOINT="https://b001991453068c59d8f4aa512bc0b16a.r2.cloudflarestorage.com"
R2_BUCKET="harboros-update"
RELEASES_KEY="scale/${train}/releases.json"

mkdir -p ./tmp-update-releases

echo "Downloading existing releases.json from R2..."
/usr/local/bin/aws s3api get-object --endpoint-url ${R2_ENDPOINT} --bucket ${R2_BUCKET} --key "${RELEASES_KEY}" ./tmp-update-releases/releases.json || echo "{}" > ./tmp-update-releases/releases.json

echo "Reading checksum from ./tmp/release/${updateFileName}.update.sha256..."
CHECKSUM=$(cat "./tmp/release/${updateFileName}.update.sha256" | awk '{print $1}')

echo "Getting file size..."
FILESIZE=$(stat --format=%s "./tmp/release/${updateFileName}.update")

echo "Generating current date..."
CURRENT_DATE=$(date -u +"%Y-%m-%dT%H:%M:%S.%6N")

echo "Determining release profile based on train..."
if [[ "$train" == *-Nightlies ]]; then
  PROFILE="DEVELOPER"
elif [[ "$train" == *-BETA || "$train" == *-RC ]]; then
  PROFILE="EARLY_ADOPTER"
else
  PROFILE="GENERAL"
fi

echo "Creating new release entry..."
NEW_ENTRY=$(cat <<EOF
{
"${TRUENAS_VERSION}": {
    "filename": "${updateFileName}.update",
    "version": "${TRUENAS_VERSION}",
    "date": "${CURRENT_DATE}",
    "changelog": "",
    "checksum": "${CHECKSUM}",
    "filesize": ${FILESIZE},
    "profile": "${PROFILE}"
}
}
EOF
)

echo "Merging with existing releases.json..."
jq -s '.[0] * .[1]' ./tmp-update-releases/releases.json <(echo "${NEW_ENTRY}") > ./tmp-update-releases/releases_updated.json

echo "Keeping only last 30 entries by date..."
jq 'to_entries | sort_by(.value.date) | .[-30:] | from_entries' ./tmp-update-releases/releases_updated.json > ./tmp-update-releases/releases_final.json

echo "Uploading updated releases.json to R2..."
/usr/local/bin/aws s3api put-object --endpoint-url ${R2_ENDPOINT} --bucket ${R2_BUCKET} --key "${RELEASES_KEY}" --body ./tmp-update-releases/releases_final.json --content-type "application/json"

echo "Successfully updated releases.json with version ${TRUENAS_VERSION}"

echo "clean up"
rm -rf ./tmp-update-releases
