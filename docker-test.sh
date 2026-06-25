#!/bin/bash

# This is for developers only to test builds.
# All volumes and containers will be deleted 
# so it builds clean for testing

if [ $# -eq 0 ] || [ -z "$1" ]; then
    clear
    echo "Error: No profile specified."
    echo "Usage: $0 <profile>"
    echo
    exit 1
fi

PROFILE=$1

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
