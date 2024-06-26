import queue
import gym
from gym import spaces
import numpy as np
import threading
import time
from atomiclong import AtomicLong
import timeit
from queue import Empty


class Packet:
    def __init__(self, time, traffic_class) -> None:
        self.start = time
        self.traffic_class = traffic_class
        pass

    def get_start(self):
        return self.start

    def get_traffic_class(self):
        return self.traffic_class


class NetworkEnv(gym.Env):
    metadata = {"render.modes": ["human"]}

    def __init__(self):
        super(NetworkEnv, self).__init__()
        # Parameters
        self.scale_factor = 10
        self.generator_setting = {
            "TF1": {"num_thread": 4, "packet_size": 1024, "rate": 100, "price": 10},
            "TF2": {"num_thread": 4, "packet_size": 1024, "rate": 200, "price": 20},
            "TF3": {"num_thread": 1, "packet_size": 10240, "rate": 1024, "price": 3},
        }
        self.processor_setting = {
            "NR": {"num_thread": 2, "limit": 200, "rate": 100, "revenue_factor": 0.9},
            "WF": {"num_thread": 1, "limit": 200, "rate": 100, "revenue_factor": 0.1},
        }
        self.timeout_processor = 0.5
        self.total_simulation_time = 30  # seconds
        self.traffic_classes = list(self.generator_setting.keys())
        self.choices = list(self.processor_setting.keys())
        self.action_space = spaces.Box(
            low=0, high=1, shape=(len(self.traffic_classes),), dtype=np.float32
        )
        self.queue = {}
        for key, value in self.processor_setting.items():
            self.queue[key] = queue.Queue(value["limit"] * value["num_thread"])

        self.observation_space = spaces.Box(low=0, high=100, dtype=np.float32)
        self.total_revenue = AtomicLong(0)

    def packet_generator(self, traffic_class, action):
        setting = self.generator_setting[traffic_class]
        packet_size_bytes = setting["packet_size"]
        target_throughput_mbps = setting["rate"]
        accum_counter = self.accumulators[traffic_class]
        time_to_wait = packet_size_bytes * 8 / (target_throughput_mbps * 1e6)
        start_time = time.time()
        proprotion_5G = action[self.traffic_classes.index(traffic_class)]
        weights = [proprotion_5G, 1 - proprotion_5G]
        print("Traffic class", traffic_class, weights)
        while True:
            accum_counter["total"] += 1
            choice = np.random.choice(a=self.choices, p=weights)
            queue = self.queue[choice]
            if queue.full():
                accum_counter["drop"] += 1
                self.stat[choice][traffic_class]["loss"] += 1
            else:
                packet = Packet(time.time_ns(), traffic_class)
                queue.put(packet)
                accum_counter[choice] += 1
            timeit.time.sleep(time_to_wait)
            if time.time() - start_time > self.total_simulation_time:
                print("Generator finish", traffic_class)
                break

    def packet_processor(self, tech, rate, queue):
        start = time.time()
        print("Processor", tech, rate, "mbps")
        while True:
            try:
                item = queue.get(timeout=self.timeout_processor)
                if item is None:
                    time.sleep(0.0001)
                    continue
                else:
                    traffic_class = item.get_traffic_class()
                    packet_size = self.generator_setting[traffic_class]["packet_size"]
                    process_time = packet_size * 1.0 * 8 / (rate * 1e6)
                    timeit.time.sleep(process_time)
                    latency = time.time_ns() - item.get_start()
                    if latency <= 0:
                        print("Negative time", latency)
                    else:
                        self.accumulators[traffic_class]["latency"].append(latency)
                        self.stat[tech][traffic_class]["revenue"] += 1
                        self.stat[tech][traffic_class]["packet_count"] += 1
                    queue.task_done()
            except Exception as error:
                if type(error) is Empty:
                    if time.time() - start > self.total_simulation_time + 2:
                        print("Processor finish", tech)
                        break
                    continue
                print("Exception", error)

    def print_stat(self):
        start = time.time()
        start_interval = start
        self.state_snapshot = {}
        while True:
            if time.time() - start_interval < 5:
                time.sleep(1)
                continue

            longest = 0
            for key, value in self.accumulators.items():
                log_str = key
                for k, v in value.items():
                    if k == "latency":
                        log_str += (
                            ". latency: " + str(round(np.mean(v) / 1e6, 2)) + " ms"
                        )
                    else:
                        throughput = self.scale_factor * (
                            v.value
                            * self.generator_setting[key]["packet_size"]
                            * 8
                            / (5 * 1e6)
                        )
                        log_str += ". " + k + ": " + str(round(throughput, 2)) + " mbps"
                        v.value = 0
                print(log_str)
                longest = max(longest, len(log_str))

            for key, value in self.stat.items():
                log_str = key
                total_revenue = 0
                total_loss = 0
                total_data = 0
                rev_factor = self.processor_setting[key]["revenue_factor"]
                for k, v in value.items():
                    tf_rev = (
                        self.generator_setting[k]["price"]
                        * self.generator_setting[k]["packet_size"]
                        * v["revenue"].value
                        / 8
                        / 1e6
                    )
                    tf_loss = (
                        self.generator_setting[k]["price"]
                        * self.generator_setting[k]["packet_size"]
                        * v["loss"].value
                        / 8
                        / 1e6
                    )
                    total_revenue += self.scale_factor * tf_rev
                    total_loss += self.scale_factor * tf_loss
                    total_data += self.scale_factor * (
                        v["packet_count"].value
                        * self.generator_setting[k]["packet_size"]
                        * 8
                    )
                    throughput = self.scale_factor * (
                        v["packet_count"].value
                        * self.generator_setting[k]["packet_size"]
                        * 8
                        / (5 * 1e6)
                    )
                    log_str += (
                        ". "
                        + k
                        + ". R: "
                        + str(round(tf_rev, 2))
                        + "$. L: "
                        + str(round(tf_loss, 2))
                        + "$. T: "
                        + str(round(throughput, 2))
                        + " mbps"
                    )
                    v["packet_count"].value = 0
                log_str += (
                    "|All. R: "
                    + str(round(total_revenue * rev_factor, 2))
                    + "$. L: "
                    + str(round(total_loss * rev_factor, 2))
                    + "$. T: "
                    + str(round(total_data / (5 * 1e6), 2))
                    + " mbps"
                )
                print(log_str)
                longest = max(longest, len(log_str))

            separator = "=" * longest
            print("Queue.", self.get_queue_status())
            print(separator)
            start_interval = time.time()
            if time.time() - start > (self.total_simulation_time):
                print("Stop monitor")
                break

    def get_queue_status(self):
        result = ", ".join(
            map(
                lambda kv: f"{kv[0]}: {str(round(kv[1].qsize()/kv[1].maxsize,2))}",
                self.queue.items(),
            )
        )
        return result

    def step(self, action):
        start_step = time.time()
        self.generators = {}
        self.accumulators = {}
        self.stat = {}
        list_generator_threads = []
        list_processor_threads = []
        for key, value in self.processor_setting.items():
            my_queue = self.queue[key]
            self.stat[key] = {}
            for i in range(value["num_thread"]):
                processor = threading.Thread(
                    target=self.packet_processor,
                    args=(
                        key,
                        value["rate"],
                        my_queue,
                    ),
                )
                list_processor_threads.append(processor)

            for tf in self.generator_setting.keys():
                self.stat[key][tf] = {
                    "revenue": AtomicLong(0),
                    "packet_count": AtomicLong(0),
                    "loss": AtomicLong(0),
                }

        for key, value in self.generator_setting.items():
            self.accumulators[key] = {}
            self.accumulators[key]["total"] = AtomicLong(0)
            self.accumulators[key]["drop"] = AtomicLong(0)
            self.accumulators[key]["latency"] = []

            for val in self.choices:
                self.accumulators[key][val] = AtomicLong(0)

            for i in range(value["num_thread"]):
                packet_generator_thread = threading.Thread(
                    target=self.packet_generator,
                    args=(
                        key,
                        action,
                    ),
                )
                list_generator_threads.append(packet_generator_thread)

        for t in list_processor_threads:
            t.start()

        for t in list_generator_threads:
            t.start()

        log_thread = threading.Thread(target=self.print_stat)
        log_thread.start()

        for t in list_processor_threads:
            t.join()

        for t in list_generator_threads:
            t.join()

        log_thread.join()

        print(
            "Finish step. Queue {" + self.get_queue_status() + "}.",
            "Total time:",
            str(round(time.time() - start_step, 2)),
            "s",
        )
        return [], 0, False, {}

    def reset(self):
        print("Reset not implemented")
        pass

    def render(self, mode="human"):
        print("Render not implemented")
        pass

    def close(self):
        pass


env = NetworkEnv()
observation = env.reset()
# action = env.action_space.sample()
action = [0.5, 1, 0.9]
observation, reward, done, _ = env.step(action)
# action = [1, 1, 1]
# NR. TF1. R: 63.28$. L: 11.38$. T: 143.88 mbps. TF2. R: 221.95$. L: 33.4$. T: 251.45 mbps. TF3. R: 10.18$. L: 3.46$. T: 77.99 mbps|All. R: 2954.07$. L: 482.42$. T: 473.32 mbps
# action = [1, 0.5, 1]
# NR. TF1. R: 70.45$. L: 0.0$. T: 145.44 mbps. TF2. R: 117.94$. L: 0.0$. T: 121.31 mbps. TF3. R: 13.43$. L: 0.0$. T: 94.54 mbps|All. R: 2018.15$. L: 0.0$. T: 361.28 mbps
