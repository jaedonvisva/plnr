#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for the plnr bot. Run as root, over ssh.
#
#   ssh -i ~/.ssh/plnr_ed25519 root@IP 'bash -s' < deploy/setup-server.sh            # step 1
#   ssh -i ~/.ssh/plnr_ed25519 admin@IP true                                         # prove admin works
#   ssh -i ~/.ssh/plnr_ed25519 admin@IP 'sudo bash -s -- lockdown' < deploy/setup-server.sh   # step 2
#
# Step 1 (default) creates the users, firewall and swap, and leaves root login untouched so a
# mistake can't lock you out. Step 2 ("lockdown") disables root and password logins; only run it
# after the admin login has worked. Both steps are safe to re-run.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
[ "$(id -u)" = 0 ] || { echo "run as root (or via sudo)"; exit 1; }

if [ "${1:-setup}" = "lockdown" ]; then
  # sshd uses the FIRST value it sees, and Ubuntu's own drop-ins sort before "99-", so ours is 00-.
  cat > /etc/ssh/sshd_config.d/00-plnr.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
CONF
  sshd -t                                  # refuse to reload a broken config
  systemctl reload ssh
  echo "effective sshd settings:"
  sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
  exit 0
fi

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip rsync ufw unattended-upgrades tzdata >/dev/null

echo "==> users (admin: sudo; plnr: runs the bot, no sudo)"
for u in admin plnr; do
  if ! id "$u" >/dev/null 2>&1; then
    # Ubuntu already has an "admin" group, which makes a plain `adduser admin` fail.
    if getent group "$u" >/dev/null; then group_arg=(--ingroup "$u"); else group_arg=(); fi
    adduser --disabled-password --gecos "" ${group_arg[@]+"${group_arg[@]}"} "$u" >/dev/null
  fi
done
usermod -aG sudo admin
echo 'admin ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-admin
chmod 440 /etc/sudoers.d/90-admin
visudo -cf /etc/sudoers.d/90-admin >/dev/null

echo "==> ssh keys (copied from root's authorized_keys)"
[ -s /root/.ssh/authorized_keys ] || { echo "root has no authorized_keys to copy"; exit 1; }
for u in admin plnr; do
  install -d -m 700 -o "$u" -g "$u" "/home/$u/.ssh"
  install -m 600 -o "$u" -g "$u" /root/.ssh/authorized_keys "/home/$u/.ssh/authorized_keys"
done
install -d -m 750 -o plnr -g plnr /home/plnr/plnr

echo "==> firewall (ssh only)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

echo "==> swap"
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo
echo "setup done. Next: ssh -i ~/.ssh/plnr_ed25519 admin@<IP> true   (must succeed), then run the lockdown step."
