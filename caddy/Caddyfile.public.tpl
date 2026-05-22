# Public internet — rendered by scripts/render-caddyfile.sh (do not edit generated file).
# Requires: DOMAIN, ACME_EMAIL, WEB_AUTH_USER, WEB_AUTH_HASH in .env
# Router: forward WAN TCP 80 + 443 → this iMac.

{
	email ${ACME_EMAIL}
}

${DOMAIN} {
	basicauth {
		${WEB_AUTH_USER} ${WEB_AUTH_HASH}
	}
	reverse_proxy web:3000
}
