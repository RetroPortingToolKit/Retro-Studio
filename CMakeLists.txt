cmake_minimum_required(VERSION 3.20)
project(retcomm-studio VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# windows.h (reached through winsock2.h in studio_debug.cpp, its test, and
# studio_snes_data.cpp) defines min and max as MACROS. One `std::min` in a TU
# that has seen it fails as "illegal token on right side of '::'", pointing at
# the call rather than at the header that broke it -- which is how
# debug_client_test.cpp broke the Windows CI build and nothing else.
#
# Set here, once, so a TU cannot inherit the trap by adding an include. The
# files that pull windows.h in still guard it themselves: they are vendored
# into other repos and cannot rely on this file being the one that builds them.
if(WIN32)
    add_compile_definitions(NOMINMAX)
endif()

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" RETCOMM_STUDIO_VERSION_FILE)
string(STRIP "${RETCOMM_STUDIO_VERSION_FILE}" RETCOMM_STUDIO_VERSION)

# ---------------------------------------------------------------------------
# Dear ImGui (SDL3 + OpenGL3) — same sourcing strategy as retcomm-hub
# ---------------------------------------------------------------------------
set(RETCOMM_IMGUI_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../recomp-ui/src/third_party/imgui"
    CACHE PATH "Path to Dear ImGui sources")

find_package(SDL3 QUIET)
find_package(OpenGL QUIET)
if(NOT TARGET OpenGL::GL)
    find_package(OpenGL REQUIRED)
endif()

if(NOT SDL3_FOUND AND NOT TARGET SDL3::SDL3)
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(SDL3 IMPORTED_TARGET sdl3)
    endif()
endif()

set(RETCOMM_HAVE_SDL3 FALSE)
set(RETCOMM_STUDIO_SDL_TARGET "")
if(TARGET SDL3::SDL3)
    set(RETCOMM_HAVE_SDL3 TRUE)
    set(RETCOMM_STUDIO_SDL_TARGET SDL3::SDL3)
elseif(TARGET PkgConfig::SDL3)
    set(RETCOMM_HAVE_SDL3 TRUE)
    set(RETCOMM_STUDIO_SDL_TARGET PkgConfig::SDL3)
endif()

if(NOT RETCOMM_HAVE_SDL3)
    message(FATAL_ERROR "retcomm-studio requires SDL3 (find_package SDL3 or pkg-config sdl3)")
endif()

set(RETCOMM_IMGUI_READY FALSE)
if(EXISTS "${RETCOMM_IMGUI_DIR}/imgui.cpp"
   AND EXISTS "${RETCOMM_IMGUI_DIR}/backends/imgui_impl_sdl3.cpp")
    set(RETCOMM_IMGUI_READY TRUE)
else()
    include(FetchContent)
    message(STATUS "retcomm-studio: fetching Dear ImGui (SDL3 backend)")
    FetchContent_Declare(retcomm_imgui_src
        GIT_REPOSITORY https://github.com/ocornut/imgui.git
        GIT_TAG v1.91.9-docking
        GIT_SHALLOW TRUE
    )
    FetchContent_GetProperties(retcomm_imgui_src)
    if(NOT retcomm_imgui_src_POPULATED)
        FetchContent_Populate(retcomm_imgui_src)
    endif()
    set(RETCOMM_IMGUI_DIR "${retcomm_imgui_src_SOURCE_DIR}" CACHE PATH
        "Path to Dear ImGui sources" FORCE)
    if(EXISTS "${RETCOMM_IMGUI_DIR}/backends/imgui_impl_sdl3.cpp")
        set(RETCOMM_IMGUI_READY TRUE)
    endif()
endif()

if(NOT RETCOMM_IMGUI_READY)
    message(FATAL_ERROR "Dear ImGui with imgui_impl_sdl3 not available")
endif()

include(FetchContent)
FetchContent_Declare(json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG v3.11.3
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(json)

set(RETCOMM_IMGUI_FREETYPE FALSE)
set(RETCOMM_IMGUI_FREETYPE_TARGET "")
find_package(Freetype QUIET)
if(TARGET Freetype::Freetype)
    set(RETCOMM_IMGUI_FREETYPE TRUE)
    set(RETCOMM_IMGUI_FREETYPE_TARGET Freetype::Freetype)
else()
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(RETCOMM_FT2 IMPORTED_TARGET freetype2)
        if(TARGET PkgConfig::RETCOMM_FT2)
            set(RETCOMM_IMGUI_FREETYPE TRUE)
            set(RETCOMM_IMGUI_FREETYPE_TARGET PkgConfig::RETCOMM_FT2)
        endif()
    endif()
endif()

set(RETCOMM_IMGUI_SOURCES
    ${RETCOMM_IMGUI_DIR}/imgui.cpp
    ${RETCOMM_IMGUI_DIR}/imgui_draw.cpp
    ${RETCOMM_IMGUI_DIR}/imgui_tables.cpp
    ${RETCOMM_IMGUI_DIR}/imgui_widgets.cpp
    ${RETCOMM_IMGUI_DIR}/backends/imgui_impl_sdl3.cpp
    ${RETCOMM_IMGUI_DIR}/backends/imgui_impl_opengl3.cpp
)
set(RETCOMM_IMGUI_FREETYPE_SRC
    "${CMAKE_CURRENT_SOURCE_DIR}/third_party/misc/freetype/imgui_freetype.cpp")
if(RETCOMM_IMGUI_FREETYPE AND EXISTS "${RETCOMM_IMGUI_FREETYPE_SRC}")
    list(APPEND RETCOMM_IMGUI_SOURCES ${RETCOMM_IMGUI_FREETYPE_SRC})
else()
    set(RETCOMM_IMGUI_FREETYPE FALSE)
endif()

add_library(retcomm_studio_imgui STATIC ${RETCOMM_IMGUI_SOURCES})
target_include_directories(retcomm_studio_imgui PUBLIC
    ${RETCOMM_IMGUI_DIR}
    ${RETCOMM_IMGUI_DIR}/backends
)
target_link_libraries(retcomm_studio_imgui PUBLIC ${RETCOMM_STUDIO_SDL_TARGET} OpenGL::GL)
if(RETCOMM_IMGUI_FREETYPE)
    target_include_directories(retcomm_studio_imgui PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/third_party
        ${CMAKE_CURRENT_SOURCE_DIR}/third_party/misc/freetype)
    target_compile_definitions(retcomm_studio_imgui PUBLIC IMGUI_ENABLE_FREETYPE)
    target_link_libraries(retcomm_studio_imgui PUBLIC ${RETCOMM_IMGUI_FREETYPE_TARGET})
    message(STATUS "retcomm-studio: ImGui FreeType enabled")
else()
    message(STATUS "retcomm-studio: ImGui FreeType disabled")
endif()

set(RETCOMM_STUDIO_SOURCES
    src/studio/studio_main.cpp
    src/studio/studio_bulk_recomp.cpp
    src/studio/studio_model.cpp
    src/studio/studio_runner.cpp
    src/studio/studio_functions.cpp
    src/studio/studio_analysis.cpp
    src/studio/studio_frames.cpp
    src/studio/studio_frames_data.cpp
    src/studio/studio_debug.cpp
    src/studio/studio_n64.cpp
    src/studio/studio_n64_data.cpp
    src/studio/studio_snes.cpp
    src/studio/studio_snes_functions.cpp
    src/studio/studio_snes_data.cpp
)

# Windows .exe icon (explorer / taskbar) from assets/retcomm-studio.ico
set(RETCOMM_STUDIO_WIN_RC "")
if(WIN32 AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/assets/retcomm-studio.ico")
    string(REPLACE "\\" "/" RETCOMM_STUDIO_ICO_PATH
           "${CMAKE_CURRENT_SOURCE_DIR}/assets/retcomm-studio.ico")
    configure_file("${CMAKE_CURRENT_SOURCE_DIR}/packaging/windows/retcomm-studio.rc.in"
                   "${CMAKE_BINARY_DIR}/retcomm-studio.rc" @ONLY)
    set(RETCOMM_STUDIO_WIN_RC "${CMAKE_BINARY_DIR}/retcomm-studio.rc")
endif()

if(WIN32)
    add_executable(retcomm-studio WIN32 ${RETCOMM_STUDIO_SOURCES} ${RETCOMM_STUDIO_WIN_RC})
    if(MSVC)
        target_link_options(retcomm-studio PRIVATE "/ENTRY:mainCRTStartup")
    endif()
else()
    add_executable(retcomm-studio ${RETCOMM_STUDIO_SOURCES})
endif()

target_include_directories(retcomm-studio PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)
# stb_image for window icon (PNG)
target_include_directories(retcomm-studio PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/third_party")
target_compile_definitions(retcomm-studio PRIVATE
    RETCOMM_STUDIO_VERSION="${RETCOMM_STUDIO_VERSION}"
)
target_link_libraries(retcomm-studio PRIVATE
    retcomm_studio_imgui
    nlohmann_json::nlohmann_json
)
# studio_debug.cpp and studio_snes_data.cpp speak Winsock, so the GUI needs
# ws2_32 exactly as the test targets do. It was missing here and still linked
# under MSVC, where SDL3's own link interface happens to drag ws2_32 in -- a
# transitive dependency this target never declared. MinGW's SDL3 does not
# export it, so the same source failed to link with a wall of undefined
# WSAStartup/socket/recv. Depend on it directly rather than on a neighbour.
if(WIN32)
    target_link_libraries(retcomm-studio PRIVATE ws2_32)
endif()

if(UNIX AND NOT APPLE)
    set_target_properties(retcomm-studio PROPERTIES INSTALL_RPATH "$ORIGIN/../lib;$ORIGIN")
endif()

# Friendly name for packaging / desktop launchers
set_target_properties(retcomm-studio PROPERTIES OUTPUT_NAME "Retro-Studio")

install(TARGETS retcomm-studio RUNTIME DESTINATION bin)
install(DIRECTORY tools/new_project_layout/
        DESTINATION share/retcomm-studio/toolkit
        PATTERN ".venv" EXCLUDE
        PATTERN "__pycache__" EXCLUDE
        PATTERN "*.pyc" EXCLUDE
        PATTERN "project_studio_repos.json" EXCLUDE
        PATTERN "project_studio_repos_snes.json" EXCLUDE
        PATTERN "project_studio_repos_n64.json" EXCLUDE
        PATTERN ".cache" EXCLUDE)
# The PSX analysis tools the Frames tab drives. Deliberately a SIBLING of the
# toolkit directory, in the source tree and here: gpu_tool_path() resolves them
# as <RETCOMM_STUDIO_TOOLKIT>/../psx_analysis, so tools/new_project_layout ->
# tools/psx_analysis and share/retcomm-studio/toolkit -> share/retcomm-studio/
# psx_analysis are the same lookup. Move one without the other and every tool
# button in Frames goes quietly grey.
install(DIRECTORY tools/psx_analysis/
        DESTINATION share/retcomm-studio/psx_analysis
        PATTERN "__pycache__" EXCLUDE
        PATTERN "*.pyc" EXCLUDE)
install(DIRECTORY assets/
        DESTINATION share/retcomm-studio/assets
        PATTERN "*.svg" EXCLUDE)
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/assets/fonts/LatoLatin-Regular.ttf")
    install(FILES
        assets/fonts/LatoLatin-Regular.ttf
        assets/fonts/LatoLatin-Bold.ttf
        assets/fonts/NOTICE.md
        DESTINATION share/retcomm-studio/fonts)
endif()
install(FILES VERSION DESTINATION share/retcomm-studio)

message(STATUS "retcomm-studio ${RETCOMM_STUDIO_VERSION}: ImGui=${RETCOMM_IMGUI_DIR}")

# ---- Functions tab data-path test -------------------------------------------
# studio_analysis.cpp is ImGui-free precisely so this can run without a GPU or
# a window. Point it at a repo that already has an analysis/ bundle:
#   ./build/analysis_load_test ~/Documents/GitHub/SomeRecomp
add_executable(analysis_load_test
    tests/analysis_load_test.cpp
    src/studio/studio_analysis.cpp
    src/studio/studio_frames_data.cpp
    src/studio/studio_debug.cpp
    src/studio/studio_model.cpp
)
target_include_directories(analysis_load_test PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(analysis_load_test PRIVATE nlohmann_json::nlohmann_json)
# It compiles studio_debug.cpp, so it needs Winsock for the same reason
# snes_load_test does. Without this the target fails to LINK on Windows only.
if(WIN32)
    target_link_libraries(analysis_load_test PRIVATE ws2_32)
endif()

# ---- Frames tab data-path test ----------------------------------------------
# studio_frames_data.cpp is ImGui-free for the same reason studio_analysis.cpp
# is. With no argument it checks embedded fixtures; with a directory it loads
# real gpu_frame_*.py output, which is what catches schema drift between the
# Python tools and this viewer:
#   ./build/frames_load_test ~/Documents/GitHub/SomeRecomp/analysis/frames
add_executable(frames_load_test
    tests/frames_load_test.cpp
    src/studio/studio_frames_data.cpp
    src/studio/studio_model.cpp
)
target_include_directories(frames_load_test PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(frames_load_test PRIVATE nlohmann_json::nlohmann_json)

# ---- SNES Diagnostics data-path test ----------------------------------------
# studio_snes_data.cpp is ImGui-free for the same reason the other data layers
# are. With no argument it checks embedded fixtures; with a host and port it
# also drives a live snesrecomp debug server over the bare line protocol, which
# is what catches protocol drift between the runtime and this viewer:
#   ./build/snes_load_test 127.0.0.1 4370
add_executable(n64_load_test
    tests/n64_load_test.cpp
    src/studio/studio_n64_data.cpp
    src/studio/studio_model.cpp
)

add_executable(snes_load_test
    tests/snes_load_test.cpp
    src/studio/studio_snes_data.cpp
    src/studio/studio_model.cpp
)
target_include_directories(snes_load_test PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(snes_load_test PRIVATE nlohmann_json::nlohmann_json)
if(WIN32)
    target_link_libraries(snes_load_test PRIVATE ws2_32)
endif()

target_include_directories(n64_load_test PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(n64_load_test PRIVATE nlohmann_json::nlohmann_json)
if(WIN32)
    target_link_libraries(n64_load_test PRIVATE ws2_32)
endif()

# ---- detached-launch test ----------------------------------------------------
# No GPU, no window, no game: spawns /bin/sh and checks the log redirection and
# process lifetime the Frames tab's Launch button relies on.
#   ./build/spawn_test
if(NOT WIN32)
    add_executable(spawn_test
        tests/spawn_test.cpp
        src/studio/studio_runner.cpp
        src/studio/studio_model.cpp
    )
    target_include_directories(spawn_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_link_libraries(spawn_test PRIVATE nlohmann_json::nlohmann_json)
endif()

# ---- pipe-drain test ---------------------------------------------------------
# A child's stdout and stderr must be drained CONCURRENTLY. Draining stdout to
# EOF first deadlocks past one pipe buffer of stderr, and below that still
# withholds every stderr line until the child exits -- which is what made a
# MinGW cross-build look frozen. No GPU, no repo:  ./build/pipe_drain_test
if(NOT WIN32)
    add_executable(pipe_drain_test
        tests/pipe_drain_test.cpp
        src/studio/studio_runner.cpp
        src/studio/studio_model.cpp
    )
    target_include_directories(pipe_drain_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_link_libraries(pipe_drain_test PRIVATE nlohmann_json::nlohmann_json)
endif()

# ---- JSON-boundary test ------------------------------------------------------
# The toolkit's optional fields are Python `str | None`, and nlohmann's value()
# throws on a present-but-null key rather than using the fallback. That is a
# parse failure of the WHOLE document over one field, so it gets its own test.
# No GPU, no repo:  ./build/json_null_test
add_executable(json_null_test
    tests/json_null_test.cpp
    src/studio/studio_runner.cpp
    src/studio/studio_model.cpp
)
target_include_directories(json_null_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(json_null_test PRIVATE nlohmann_json::nlohmann_json)

# ---- platform-split tests (Python) -------------------------------------------
# The cartridge halves of Studio are decided in the toolkit, not in C++: which
# repo index, which framework submodule, which scaffolder, and each console's
# migration ops. They need no GPU and no game — only git and a temp dir:
#   python3 tests/snes_platform_test.py
#   python3 tests/n64_platform_test.py
#   python3 tests/bulk_recomp_test.py
find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
    add_custom_target(snes_platform_test
        COMMAND ${Python3_EXECUTABLE}
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/snes_platform_test.py"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        COMMENT "Running SNES platform-split test"
        VERBATIM)
    add_custom_target(n64_platform_test
        COMMAND ${Python3_EXECUTABLE}
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/n64_platform_test.py"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        COMMENT "Running N64 platform-split test"
        VERBATIM)
    add_custom_target(bulk_recomp_test
        COMMAND ${Python3_EXECUTABLE}
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/bulk_recomp_test.py"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        COMMENT "Running Bulk Recomp orchestration test"
        VERBATIM)
endif()

# ---- live debug-client test --------------------------------------------------
# Speaks the debug protocol to a stub server on an ephemeral port, so it needs
# neither a running game nor a GPU:  ./build/debug_client_test
add_executable(debug_client_test
    tests/debug_client_test.cpp
    src/studio/studio_debug.cpp
)
target_include_directories(debug_client_test PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(debug_client_test PRIVATE nlohmann_json::nlohmann_json)
find_package(Threads REQUIRED)
target_link_libraries(debug_client_test PRIVATE Threads::Threads)
if(WIN32)
    target_link_libraries(debug_client_test PRIVATE ws2_32)
endif()
