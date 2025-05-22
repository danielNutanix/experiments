import nutanix_era.common.generic.GenericUtil as GenericUtil
from multiprocessing import Process, Queue
import json
import os
import time
import csv

generic_util = GenericUtil.GenericUtil()

def write_to_file(queue, result_queue, writers_count):
    while True:
        data = queue.get()
        if data is None:
            break
        if isinstance(data, dict):
            data = json.dumps(data, indent=4, sort_keys=True, ensure_ascii=False)

        data_size = len(data)
        start_time = time.time()
        # Atomic write to file
        elapsed_time_write, elapsed_time_flush, elapsed_time_fsync = generic_util.write_config_file_atomic("test.json", data)
        end_time = time.time()
        time_elapsed_total = end_time - start_time        
        result_queue.put([writers_count, data_size, elapsed_time_write*1000, elapsed_time_flush*1000, elapsed_time_fsync*1000, time_elapsed_total*1000])
        print(f"Writers: {writers_count} Data Size: {data_size} bytes, Write Time: {elapsed_time_write} ms, Flush Time: {elapsed_time_flush} ms, Fsync Time: {elapsed_time_fsync} ms, Total Time: {time_elapsed_total} ms")

def run_test_with_writer_count(writers_count):
    print(f"\n=== Running test with {writers_count} writers ===\n")
    
    queue = Queue()
    result_queue = Queue()
    processes = []
    
    # Add data to the queue
    for i in range(10000, 1000000, 10000):
        large_dict = {i: i**2 for i in range(i)}
        json_text = json.dumps(large_dict, indent=4, sort_keys=True, ensure_ascii=False)
        queue.put(json_text)
    
    # Start worker processes
    for _ in range(writers_count):
        p = Process(target=write_to_file, args=(queue, result_queue, writers_count))
        p.start()
        processes.append(p)
    
    # Stop workers
    for _ in processes:
        queue.put(None)
    for p in processes:
        p.join()
    
    # Collect results
    results = []
    while not result_queue.empty():
        results.append(result_queue.get())
    
    return results

# Main script execution
output_file = open('load_performance_results.csv', mode='w', newline='\n')
writer = csv.writer(output_file)
writer.writerow(["Number of writers", "Data Size (bytes)", "Write Time (ms)", "Flush Time (ms)", "Fsync Time (ms)", "Total Time (ms)"])

# Define writer counts to test
writer_counts = [1, 10, 25, 50, 100]
all_results = []

# Run tests for each writer count
for count in writer_counts:
    results = run_test_with_writer_count(count)
    all_results.extend(results)

for result in all_results:
    writer.writerow(result)

output_file.close()
print("All tests have finished.")