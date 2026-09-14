# Relay HTTP HEAD probes returning 502

The relay uses the Python websockets HTTP parser, which rejects any method other than GET before the application handshake hook. nginx forwarded HEAD unchanged, so HEAD requests to the relay root and /git returned 502 although browser GET requests succeeded.

The proxy now maps only HEAD to GET in locations backed by the relay. nginx retains the original request's HEAD semantics and suppresses the response body. WebSocket GET upgrades and smart-HTTP git POST requests are unchanged.

Deployed on router (192.168.0.1): /etc/nginx/sites-enabled/poster.conf and relay.conf. The change covers /git, /relay, /.well-known/nostr.json, and relay.poster.place/. Backups are /etc/nginx/poster.conf.before-cattail-20260914 and /etc/nginx/relay.conf.before-cattail-20260914. nginx -t passed before graceful reload. Both public HEAD probes return 200 and WebSocket REQ probes receive EVENT and EOSE.

During diagnosis the relay also received a separately initiated explicit systemctl restart at 08:56:25 Mountain. Its 10-second shutdown timeout expired; replacement process began at 08:56:35 and listener opened at 08:58:19. This proxy change required no relay restart.
