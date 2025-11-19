#!/bin/bash

INSTANCE_NAME="[インスタンス名]"
ZONE_NAME="[Zone名]"

func() {
	gcloud compute instances start "$INSTANCE_NAME" --zone="$ZONE_NAME"
}

is_success=1
sleep_time=$(date +%h)
num_try=0
while [ $is_success -ne 0 ]
do
	num_try=$((num_try + 1))
	echo “start_instance.sh is try. num_try: $num_try”
	func
	is_success=$?
    if [ $is_success -ne 0 ]; then
		sleep_time=$((RANDOM % 31 + 90))
		echo “start_instance.sh is failed. sleep $sleep_time”
        sleep $sleep_time
		clear
    fi
done
end_time=$(date +%h)

echo “start_instance.sh is success. $(($end_time - $sleep_time)) hours. num_try: $num_try”
echo “start_instance.sh is done”
