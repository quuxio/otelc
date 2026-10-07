#include <chrono>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>

std::uint64_t process_order(std::uint64_t value) {
    for (int j = 0; j < 16; ++j) {
        value ^= value >> 30; value *= 0xbf58476d1ce4e5b9ULL;
        value ^= value >> 27; value *= 0x94d049bb133111ebULL;
        value ^= value >> 31;
    }
    return value;
}

int held_order() {
    std::cout << "holding" << std::endl;
    std::string release;
    std::getline(std::cin, release);
    if (release == "throw") throw std::runtime_error("order rejected");
    return 7;
}

int main() {
    std::cout << "ready" << std::endl;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "quit") return 0;
        if (line == "hold") {
            try { int result = held_order(); std::cout << "result=" << result << std::endl; }
            catch (const std::runtime_error &) { std::cout << "caught" << std::endl; }
            continue;
        }
        std::istringstream input(line);
        std::string command;
        int iterations = 0;
        input >> command >> iterations;
        if (command != "batch" || iterations < 1 || iterations > 1000000) return 2;
        std::uint64_t checksum = 0;
        const auto begin = std::chrono::steady_clock::now();
        for (int i = 0; i < iterations; ++i) checksum ^= process_order(i);
        const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - begin).count();
        std::cout << "elapsed_ns=" << elapsed << " checksum=" << checksum
            << " calls=" << iterations << std::endl;
    }
}
