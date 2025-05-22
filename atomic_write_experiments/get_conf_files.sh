#!/bin/bash

INCLUDE_PATTERNS=(
    "*.json"
    "*.conf"
)

EXCLUDE_PATTERNS=(
  "shippable_python3"
  "*.j2"
  "*.gz"
  "era_server_config/drivers_depot/"
  "upgrade/config/era_config/"
  "era_engine/drivers/nutanix_era"
  "nutanix_era"
  "era_engine/stack/"
)

OUTPUT_FILE=config_list.txt
# Join patterns into a single regex
EXCLUDE_REGEX=$(printf "|%s" "${EXCLUDE_PATTERNS[@]}" | sed 's/|//' | sed 's/\./\\./g; s/\*/.*/g')
EXCLUDE_FULL_REGEX=".*/(${EXCLUDE_REGEX})"

INCLUDE_REGEX=$(printf "|%s" "${INCLUDE_PATTERNS[@]}" | sed 's/|//' | sed 's/\./\\./g; s/\*/.*/g')
INCLUDE_FULL_REGEX=".*/(${INCLUDE_REGEX})"

# Run find and include then exclude patterns
echo "Printing all config files found"
echo "==============================="
find . -type f  | grep -E "$INCLUDE_FULL_REGEX" | grep -Ev "$EXCLUDE_FULL_REGEX" | while read -r file; do basename "$file"; done | sort | uniq | tee $OUTPUT_FILE
echo "==============================="
echo "Printing all config files found done --> stored in $OUTPUT_FILE"

# scp $OUTPUT_FILE <username>@<ubvm>:~

# Correlate with codebase in UBVM
OUTPUT_FILE=config_list.txt
mapfile -t config_files < $OUTPUT_FILE


for config_file in "${config_files[@]}"; do
    echo "$config_file"
    # Search for references to the base file name
    results=$(grep -rn "$config_file" * 2>/dev/null | grep -v "Binary file" | grep -v "$OUTPUT_FILE")
    if [[ -n "$results" ]]; then
        echo $config_file | tee -a found.txt 
        while IFS= read -r ref; do
            echo "$config_file is found in file $ref"
        done <<< "$results"
    else
        echo $config_file | tee -a not_found.txt
    fi
done
