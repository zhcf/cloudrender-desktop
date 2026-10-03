cmake_minimum_required(VERSION 3.16)
project(cloudrender_nativecore LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(MSVC)
  add_compile_options(/utf-8 /W3)
endif()

if(NOT WIN32)
  message(FATAL_ERROR "nativecore v1 仅支持 Windows(DXGI/SendInput)")
endif()

add_library(nativecore SHARED
  src/cr_impl.cpp
  src/cr_capture.cpp
  src/cr_inject.cpp
)

target_include_directories(nativecore PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
# NOMINMAX:阻止 windows.h 定义 min/max 宏(否则破坏 std::min/std::max)
target_compile_definitions(nativecore PRIVATE CR_NATIVECORE_BUILD NOMINMAX)

target_link_libraries(nativecore PRIVATE d3d11 dxgi dwmapi user32)

# 输出与 SDK/示例一致的布局:build/<config>/nativecore.dll(.lib/.exp)
set_target_properties(nativecore PROPERTIES
  PREFIX ""         # 输出 nativecore.dll 而非 libnativecore.dll(Windows MSVC 默认无前缀)
  OUTPUT_NAME "nativecore"
)

# cloudrender_nativecore.* 别名副本:SDK 的 find_library 按该名查找导入库。
# 导入库内部记录的 DLL 名仍为 nativecore.dll,运行时依赖名不变。
add_custom_command(TARGET nativecore POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
          "$<TARGET_FILE:nativecore>" "$<TARGET_FILE_DIR:nativecore>/cloudrender_nativecore.dll"
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
          "$<TARGET_LINKER_FILE:nativecore>" "$<TARGET_FILE_DIR:nativecore>/cloudrender_nativecore.lib"
  COMMENT "复制 nativecore 产物别名 cloudrender_nativecore.dll/.lib"
)