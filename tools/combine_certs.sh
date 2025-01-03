#!/bin/bash
#
# Copyright (c) 2017 Angus Ainslie 
# Mods for Mail in a Box 2024
#

IN_PATH=$1
CERT_NAME_FULL=$2
CERT_NAME_PRIV=$3
OUT_PATH=$4
PEM_NAME=$5

CHAIN_SUM=`md5sum ${IN_PATH}/${CERT_NAME_FULL}`
KEY_SUM=`md5sum ${IN_PATH}/${CERT_NAME_PRIV}`

echo "Chain sum ${CHAIN_SUM}"
echo "Key sum ${KEY_SUM}"

if [ ! -e ${OUT_PATH}/sums ]; then
  touch ${OUT_PATH}/sums
fi

md5sum --status -c ${OUT_PATH}/sums

if [ $? -eq 0 ]; then
  echo "Keys match"
else
  echo "Keys don't match. re-creating pem file"
  cat ${IN_PATH}/${CERT_NAME_FULL} ${IN_PATH}/${CERT_NAME_PRIV} > ${OUT_PATH}/${PEM_NAME}
  echo ${CHAIN_SUM} > ${OUT_PATH}/sums
  echo ${KEY_SUM} >> ${OUT_PATH}/sums
fi
