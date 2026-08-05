#!/bin/bash
while IFS='=' read -r key value; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    if [ -z "$value" ]; then
        echo "$key = (EMPTY)"
    elif [ ${#value} -le 6 ]; then
        echo "$key = ***"
    else
        echo "$key = ${value:0:2}***${value: -2}"
    fi
done < .env
