#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
 g++ -std=c++17 -O3 -DNDEBUG -march=native -flto -Wall -Wextra cpp/main.cpp cpp/graph.cpp -o build/envitop-cpp $(pkg-config --cflags --libs ncursesw) -lsqlite3
printf 'Built %s\n' "$PWD/build/envitop-cpp"
