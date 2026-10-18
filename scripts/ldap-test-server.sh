#!/usr/bin/env bash
# A throwaway OpenLDAP (slapd) for tests/test_ldap_real.py: real LDAP protocol, no Docker needed.
#   sudo apt-get install -y slapd ldap-utils     (Debian/Ubuntu)
#   scripts/ldap-test-server.sh                  -> ldap://127.0.0.1:3389, base dc=example,dc=org
#   FBADMIN_TEST_LDAP_URL=ldap://127.0.0.1:3389 pytest backend/tests/test_ldap_real.py
# Directory: uid=alice (admin, in cn=fbadmins) and uid=bob under ou=people; passwords alice-pw / bob-pw;
# service account cn=admin,dc=example,dc=org / adminpw.
set -euo pipefail
PORT="${LDAP_PORT:-3389}"
dir="${LDAP_DIR:-${TMPDIR:-/tmp}/fbadmin-ldap}"
SLAPD="$(command -v slapd || echo /usr/sbin/slapd)"
schema=/etc/ldap/schema
[ -d "$schema" ] || schema=/etc/openldap/schema
if [ -f "$dir/slapd.pid" ]; then kill "$(cat "$dir/slapd.pid")" 2>/dev/null || true; sleep 1; fi
rm -rf "$dir" && mkdir -p "$dir/db"
modpath="$(dirname "$(find /usr/lib -name back_mdb.so 2>/dev/null | head -1)" 2>/dev/null || true)"
cat > "$dir/slapd.conf" <<CONF
include $schema/core.schema
include $schema/cosine.schema
include $schema/inetorgperson.schema
pidfile $dir/slapd.pid
$( [ -n "$modpath" ] && printf 'modulepath %s\nmoduleload back_mdb\n' "$modpath" )
database mdb
maxsize 10485760
suffix "dc=example,dc=org"
rootdn "cn=admin,dc=example,dc=org"
rootpw adminpw
directory $dir/db
access to attrs=userPassword by self write by anonymous auth by * none
access to * by * read
CONF
hash() { slappasswd -s "$1"; }
cat > "$dir/seed.ldif" <<LDIF
dn: dc=example,dc=org
objectClass: dcObject
objectClass: organization
o: Example
dc: example

dn: ou=people,dc=example,dc=org
objectClass: organizationalUnit
ou: people

dn: ou=groups,dc=example,dc=org
objectClass: organizationalUnit
ou: groups

dn: uid=alice,ou=people,dc=example,dc=org
objectClass: inetOrgPerson
uid: alice
cn: Alice
sn: Admin
mail: alice@example.org
userPassword: $(hash alice-pw)

dn: uid=bob,ou=people,dc=example,dc=org
objectClass: inetOrgPerson
uid: bob
cn: Bob
sn: User
mail: bob@example.org
userPassword: $(hash bob-pw)

dn: cn=fbadmins,ou=groups,dc=example,dc=org
objectClass: groupOfNames
cn: fbadmins
member: uid=alice,ou=people,dc=example,dc=org
LDIF
"$SLAPD" -f "$dir/slapd.conf" -h "ldap://127.0.0.1:$PORT/"
for _ in $(seq 1 30); do (echo >/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null && break; sleep 0.2; done
ldapadd -x -H "ldap://127.0.0.1:$PORT" -D cn=admin,dc=example,dc=org -w adminpw -f "$dir/seed.ldif" >/dev/null
echo "slapd on ldap://127.0.0.1:$PORT (dc=example,dc=org)"
