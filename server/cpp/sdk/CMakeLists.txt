# CloudRender C++ SDK(nativecore 零拷贝会话层)
cmake_minimum_required(VERSION 3.16)
project(cloudrender_sdk LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(MSVC)
  add_compile_options(/W3 /utf-8)
else()
  add_compile_options(-Wall -Wextra)
endif()

# ----------------------------------------------------------------------
# cr_wire:统一协议层(纯标准库,任何平台/编译器可构建)
# ----------------------------------------------------------------------
add_library(cr_wire STATIC
    src/wire/cr_wire.cpp)
target_include_directories(cr_wire PUBLIC include)

# ----------------------------------------------------------------------
# cr_session_core:nativecore 适配器 + WebRTC 会话层
#   - 默认:协议层 + 适配器(需要 nativecore 头文件)
#   - CR_BUILD_WEBRTC=ON:同库开启 libwebrtc 会话实现(需预编译 libwebrtc)
# ----------------------------------------------------------------------
set(CR_NATIVECORE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../nativecore"
    CACHE PATH "nativecore 源码根目录(提供 include/cloudrender/capi/cr_api.h)")

if(EXISTS "${CR_NATIVECORE_ROOT}/include/cloudrender/capi/cr_api.h")
  add_library(cr_session_core STATIC
      src/session/cr_webrtc_session.cpp
      src/session/nativecore_source.cpp)
  target_include_directories(cr_session_core PUBLIC include)
  target_include_directories(cr_session_core PRIVATE "${CR_NATIVECORE_ROOT}/include")
  target_link_libraries(cr_session_core PUBLIC cr_wire)

  if(WIN32)
    find_library(NATIVECORE_IMPLIB NAMES cloudrender_nativecore
                 PATHS "${CR_NATIVECORE_ROOT}/build"
                       "${CR_NATIVECORE_ROOT}/build/Release"
                       "${CR_NATIVECORE_ROOT}/build/Debug"
                 NO_DEFAULT_PATH)
    if(NATIVECORE_IMPLIB)
      target_link_libraries(cr_session_core PUBLIC ${NATIVECORE_IMPLIB})
    else()
      message(STATUS "cloudrender_nativecore 导入库未找到:SDK 可编译,链接使用方需自行提供")
    endif()
  endif()
else()
  message(WARNING "未找到 ${CR_NATIVECORE_ROOT}/include/cloudrender/capi/cr_api.h,跳过 cr_session_core")
endif()

# ----------------------------------------------------------------------
# libwebrtc 会话层开关(webrtc-sdk 预编译 facade 包,非原生头文件)
# 依赖:third_party/libwebrtc/libwebrtc-x64-release/{include,lib}
# 用法:cmake -DCR_BUILD_WEBRTC=ON(自动探测;可用 -DCR_WEBRTC_ROOT=<dir> 覆盖)
# ----------------------------------------------------------------------
option(CR_BUILD_WEBRTC "用预编译 libwebrtc(webrtc-sdk facade 包)构建会话层" OFF)

if(CR_BUILD_WEBRTC)
  if(NOT TARGET cr_session_core)
    message(FATAL_ERROR "cr_session_core 不存在(nativecore 头文件缺失)")
  endif()
  set(CR_WEBRTC_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/libwebrtc/libwebrtc-x64-release"
      CACHE PATH "libwebrtc 预编译包根目录(含 include/ 与 lib/)")
  if(NOT EXISTS "${CR_WEBRTC_ROOT}/include/libwebrtc.h")
    message(FATAL_ERROR "未找到 ${CR_WEBRTC_ROOT}/include/libwebrtc.h;用 -DCR_WEBRTC_ROOT=<dir> 指定预编译包")
  endif()

  # RTC_DESKTOP_DEVICE 必须与预编译 DLL 的构建宏一致:rtc_peerconnection_factory.h
  # 中该宏会增减 GetDesktopDevice/CreateDesktopSource 两个纯虚函数,定义不一致时
  # 工厂虚表槽位错位(调错方法→参数类型混淆→访问违例崩溃)。
  target_compile_definitions(cr_session_core PUBLIC CR_HAVE_WEBRTC LIB_WEBRTC_API_DLL NOMINMAX RTC_DESKTOP_DEVICE)
  target_include_directories(cr_session_core PUBLIC "${CR_WEBRTC_ROOT}/include")
  target_link_libraries(cr_session_core PUBLIC "${CR_WEBRTC_ROOT}/lib/libwebrtc.dll.lib")
  if(WIN32)
    target_link_libraries(cr_session_core PUBLIC
        winmm ws2_32 secur32 crypt32 iphlpapi d3d11 dxgi)
  endif()

  # 运行时 DLL:复制到构建输出根目录,供上层应用/测试使用(测试目录另有局部复制)
  set(CR_WEBRTC_RUNTIME_DLL "${CR_WEBRTC_ROOT}/lib/libwebrtc.dll")
  add_custom_command(TARGET cr_session_core POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
              "${CR_WEBRTC_RUNTIME_DLL}"
              "${CMAKE_BINARY_DIR}/$<CONFIG>"
      COMMENT "复制 libwebrtc.dll → ${CMAKE_BINARY_DIR}/$<CONFIG>")

  # --------------------------------------------------------------------
  # cloudrender_session.dll:会话核心 C ABI(Python ctypes)
  #   内部含 libwebrtc 会话 + nativecore 直连抓屏/注入;信令经轮询交付
  # --------------------------------------------------------------------
  add_library(cr_session_dll SHARED src/capi/cr_session_capi.cpp)
  target_link_libraries(cr_session_dll PRIVATE cr_session_core)
  target_include_directories(cr_session_dll PRIVATE "${CR_NATIVECORE_ROOT}/include")
  target_compile_definitions(cr_session_dll PRIVATE CR_SESSION_BUILD)
  set_target_properties(cr_session_dll PROPERTIES
      OUTPUT_NAME "cloudrender_session")
  add_custom_command(TARGET cr_session_dll POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
              "${CR_WEBRTC_RUNTIME_DLL}"
              "$<TARGET_FILE_DIR:cr_session_dll>")
  if(NATIVECORE_IMPLIB)
    get_filename_component(CR_NATIVECORE_LIB_DIR "${NATIVECORE_IMPLIB}" DIRECTORY)
    # 导入库内部记录的 DLL 名是 nativecore.dll(cloudrender_nativecore.lib 仅为
    # nativecore.lib 的副本,表名不变);必须按真实名字复制,否则加载期找不到依赖。
    if(EXISTS "${CR_NATIVECORE_LIB_DIR}/nativecore.dll")
      set(CR_NATIVECORE_RUNTIME_DLL "${CR_NATIVECORE_LIB_DIR}/nativecore.dll")
    else()
      set(CR_NATIVECORE_RUNTIME_DLL "${CR_NATIVECORE_LIB_DIR}/cloudrender_nativecore.dll")
    endif()
    if(EXISTS "${CR_NATIVECORE_RUNTIME_DLL}")
      add_custom_command(TARGET cr_session_dll POST_BUILD
          COMMAND ${CMAKE_COMMAND} -E copy_if_different
                  "${CR_NATIVECORE_RUNTIME_DLL}"
                  "$<TARGET_FILE_DIR:cr_session_dll>")
    endif()
  endif()
endif()

# ----------------------------------------------------------------------
# 测试(协议层契约:与 Python golden 字节比对)
# ----------------------------------------------------------------------
enable_testing()
add_subdirectory(tests)