#!/usr/bin/env bash
# Creates the always-on worker VM and installs the desk on it. Run it in Google
# Cloud Shell (console.cloud.google.com, the >_ button at the top right):
#
#   curl -fsSLO https://raw.githubusercontent.com/quantscout-nlp/QuantScout-Cloud/recovery-desk/deploy/create_vm_gcloud.sh
#   bash create_vm_gcloud.sh
#
# Defaults: the Cloud Shell's current project, zone us-east1-b, VM name recovery-desk.
# Override with  PROJECT=... ZONE=... NAME=... bash create_vm_gcloud.sh
#
# The VM gets no Google service account (it can't touch your Cloud project), no
# open ports besides the default SSH rule, Shielded VM boot and OS Login.
set -euo pipefail

PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}
ZONE=${ZONE:-us-east1-b}
NAME=${NAME:-recovery-desk}
SETUP=https://raw.githubusercontent.com/quantscout-nlp/QuantScout-Cloud/recovery-desk/deploy/setup_vm.sh

[ -n "$PROJECT" ] || { echo "No project selected. Run: gcloud config set project YOUR_PROJECT_ID"; exit 1; }
case "$ZONE" in
  us-east1-*|us-central1-*|us-west1-*) ;;
  *) echo "The free e2-micro is only in us-east1, us-central1 or us-west1 (got $ZONE)."; exit 1 ;;
esac

echo "Project $PROJECT, zone $ZONE, VM $NAME"
gcloud services enable compute.googleapis.com --project "$PROJECT"

if ! gcloud compute instances describe "$NAME" --project "$PROJECT" --zone "$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$NAME" --project "$PROJECT" --zone "$ZONE" \
    --machine-type e2-micro \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size 30GB --boot-disk-type pd-standard \
    --no-service-account --no-scopes \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --metadata enable-oslogin=TRUE
fi

# The VM needs a minute to boot before SSH answers.
for i in 1 2 3 4 5 6 7 8 9 10; do
  if gcloud compute ssh "$NAME" --project "$PROJECT" --zone "$ZONE" --quiet \
       --command "curl -fsSLO $SETUP && sudo bash setup_vm.sh"; then
    break
  fi
  [ "$i" = 10 ] && { echo "SSH didn't come up. Re-run this script in a minute."; exit 1; }
  echo "Waiting for the VM to accept SSH ($i/10)..."; sleep 15
done

cat <<NEXT

Installed. Two steps left:
  1. gcloud compute ssh $NAME --zone $ZONE
     sudo nano /etc/recovery-desk.env      (paste the desk values, no DESK_ prefix; Ctrl+O, Enter, Ctrl+X)
  2. sudo systemctl enable --now recovery-desk && journalctl -u recovery-desk -f
NEXT
