# 协议契约测试:cr_wire(本语言)↔ Python 参考实现(golden.txt 由 tools/golden_wire.py 生成)
add_executable(cr_contract_test contract_test.cpp)
target_link_libraries(cr_contract_test PRIVATE cr_wire)

add_test(NAME cr_wire_contract
         COMMAND cr_contract_test "${CMAKE_CURRENT_SOURCE_DIR}/golden.txt")

# WebRTC 会话层冒烟测试(仅 CR_BUILD_WEBRTC=ON 时构建):
# 假帧源/假注入器驱动 PeerSession,验证工厂初始化 + offer 生成(m=video/m=application)
if(CR_BUILD_WEBRTC AND DEFINED CR_WEBRTC_RUNTIME_DLL)
  add_executable(cr_webrtc_smoke webrtc_smoke.cpp)
  target_link_libraries(cr_webrtc_smoke PRIVATE cr_session_core)
  add_custom_command(TARGET cr_webrtc_smoke POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
              "${CR_WEBRTC_RUNTIME_DLL}"
              "$<TARGET_FILE_DIR:cr_webrtc_smoke>")
  add_test(NAME cr_webrtc_smoke COMMAND cr_webrtc_smoke)

  # facade 分步探针(诊断用,不注册为测试)
  add_executable(cr_webrtc_probe webrtc_probe.cpp)
  target_link_libraries(cr_webrtc_probe PRIVATE cr_session_core)
  add_custom_command(TARGET cr_webrtc_probe POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
              "${CR_WEBRTC_RUNTIME_DLL}"
              "$<TARGET_FILE_DIR:cr_webrtc_probe>")
endif()