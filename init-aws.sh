#!/bin/bash
awslocal kinesis create-stream \
    --stream-name aurora-news-stream \
    --shard-count 1