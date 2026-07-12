// PosterChanAI built-in TURN + STUN server.
//
// A tiny, self-contained relay for the voice/video call feature — the modern replacement for coturn.
// It is NOT a standalone service you configure: the app supervises it as a subprocess (app/services/
// turn_service.py), passing config via env, exactly like the botframework subprocess.
//
// Auth is the standard "TURN REST API" ephemeral-credential scheme (a.k.a. coturn's use-auth-secret):
//
//	username   = "<unix-expiry>[:<userid>]"
//	credential = base64( HMAC_SHA1( shared_secret, username ) )
//
// FastAPI (app/routers/calls.py) mints exactly this from the same PC_TURN_SECRET, so the two agree with
// zero shared state — no per-user config, no static passwords, credentials expire on their own.
//
// It serves STUN (binding requests) on the same sockets, so P2P-first calls need no separate STUN server.
package main

import (
	"crypto/hmac"
	"crypto/sha1" //nolint:gosec // SHA1-HMAC is the TURN REST API standard (coturn use-auth-secret), not used for integrity
	"crypto/tls"
	"encoding/base64"
	"log"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/pion/turn/v4"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)

	publicIP := os.Getenv("PC_TURN_PUBLIC_IP") // the server's PUBLIC address, advertised in relay candidates
	secret := os.Getenv("PC_TURN_SECRET")      // shared with FastAPI's credential minting
	realm := env("PC_TURN_REALM", "posterchan")
	port := envInt("PC_TURN_PORT", 3478)      // UDP + TCP
	tlsPort := envInt("PC_TURN_TLS_PORT", 0)  // 0 = disabled; 443 recommended for restrictive/mobile networks
	certFile := os.Getenv("PC_TURN_TLS_CERT") // for turns:// on tlsPort
	keyFile := os.Getenv("PC_TURN_TLS_KEY")
	minPort := envInt("PC_TURN_MIN_PORT", 49160)
	maxPort := envInt("PC_TURN_MAX_PORT", 49200) // small default range → few open ports to forward; widen for scale

	if secret == "" {
		log.Fatal("[turn] PC_TURN_SECRET is required")
	}
	if publicIP == "" {
		log.Fatal("[turn] PC_TURN_PUBLIC_IP is required (the public IP advertised in relay candidates)")
	}

	// Validate an ephemeral credential and hand pion the long-term auth key.
	// pion calls this on every allocation; we re-derive the password from the secret and compare via the
	// key it expects (MD5(username:realm:password)), so a client whose password came from FastAPI matches.
	authHandler := func(username, realm string, _ net.Addr) ([]byte, bool) {
		tsPart := username
		if i := strings.IndexByte(username, ':'); i >= 0 {
			tsPart = username[:i]
		}
		expiry, err := strconv.ParseInt(tsPart, 10, 64)
		if err != nil || expiry < time.Now().Unix() {
			return nil, false // malformed or expired credential
		}
		mac := hmac.New(sha1.New, []byte(secret))
		mac.Write([]byte(username))
		password := base64.StdEncoding.EncodeToString(mac.Sum(nil))
		return turn.GenerateAuthKey(username, realm, password), true
	}

	relayGen := &turn.RelayAddressGeneratorPortRange{
		RelayAddress: net.ParseIP(publicIP),
		Address:      "0.0.0.0",
		MinPort:      uint16(minPort),
		MaxPort:      uint16(maxPort),
	}
	if relayGen.RelayAddress == nil {
		log.Fatalf("[turn] PC_TURN_PUBLIC_IP %q is not a valid IP", publicIP)
	}

	// UDP (primary) + TCP (fallback through TCP-only networks) on the same port.
	udpConn, err := net.ListenPacket("udp4", "0.0.0.0:"+strconv.Itoa(port))
	if err != nil {
		log.Fatalf("[turn] listen udp :%d: %v", port, err)
	}
	tcpListener, err := net.Listen("tcp4", "0.0.0.0:"+strconv.Itoa(port))
	if err != nil {
		log.Fatalf("[turn] listen tcp :%d: %v", port, err)
	}

	packetConns := []turn.PacketConnConfig{{PacketConn: udpConn, RelayAddressGenerator: relayGen}}
	listeners := []turn.ListenerConfig{{Listener: tcpListener, RelayAddressGenerator: relayGen}}

	// Optional TURN-over-TLS (turns://) on tlsPort — traverses firewalls that only allow 443.
	if tlsPort > 0 && certFile != "" && keyFile != "" {
		cert, cerr := tls.LoadX509KeyPair(certFile, keyFile)
		if cerr != nil {
			log.Fatalf("[turn] load TLS cert: %v", cerr)
		}
		tlsListener, lerr := tls.Listen("tcp4", "0.0.0.0:"+strconv.Itoa(tlsPort), &tls.Config{
			Certificates: []tls.Certificate{cert},
			MinVersion:   tls.VersionTLS12,
		})
		if lerr != nil {
			log.Fatalf("[turn] listen tls :%d: %v", tlsPort, lerr)
		}
		listeners = append(listeners, turn.ListenerConfig{Listener: tlsListener, RelayAddressGenerator: relayGen})
		log.Printf("[turn] TURN-over-TLS enabled on :%d", tlsPort)
	}

	server, err := turn.NewServer(turn.ServerConfig{
		Realm:             realm,
		AuthHandler:       authHandler,
		PacketConnConfigs: packetConns,
		ListenerConfigs:   listeners,
	})
	if err != nil {
		log.Fatalf("[turn] NewServer: %v", err)
	}
	log.Printf("[turn] STUN+TURN listening on :%d (udp+tcp), relay %s:%d-%d, realm %q",
		port, publicIP, minPort, maxPort, realm)

	// Clean shutdown on SIGTERM/SIGINT (the supervisor sends SIGTERM).
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)
	<-sigs
	log.Print("[turn] shutting down")
	_ = server.Close()
}
