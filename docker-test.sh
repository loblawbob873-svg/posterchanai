#!/bin/bash

# This is for developers only to test builds.
# All volumes and containers will be deleted 
# so it builds clean for testing

PROFILE=nostr

clear

cleanup(){
	echo "Cleaning Up First....."
	docker compose --profile $PROFILE down
	docker volume rm posterchanai_pc-data posterchanai_pc-rag posterchanai_pc-pg 
	docker volume prune --all -f
}



build(){
	echo "Building....."
	git pull
	docker compose --profile $PROFILE build
	docker compose --profile $PROFILE up -d
	docker logs -f posterchanai-nostr-1
}

git pull
cleanup
build
cleanup
