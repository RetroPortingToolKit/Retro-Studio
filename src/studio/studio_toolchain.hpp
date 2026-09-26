#pragma once

// studio_toolchain.hpp — the n64lle "Toolchain" section: one explicit path per
// tool (Python, C/C++ compiler, CMake, Ninja, Cargo, Git, gh).
//
// Two surfaces share one widget:
//   * Build tab   — the SELECTED PORT's choices, stored in its
//                   tools/toolchain.cmake: the same file n64lle's
//                   build_framework.sh / setup_project.sh read and the port's
//                   configure loads with -C, so a terminal build and a Studio
//                   build of one port use the same tools.
//   * New Project — the choices a scaffold is cut with, prefilled from the
//                   Studio-wide default (studio.json "n64_toolchain") and
//                   passed to the CLI as --tool KEY=PATH.
//
// Nothing here resolves, validates or spells a path. The CLI does
// (`project_studio toolchain show|set|detect`, n64_toolchain.py), so the Build
// tab and a headless build can never disagree about which cc a configure gets.
// This file is a viewer and an editor of that answer.

#include "studio/studio_model.hpp"
#include "studio/studio_theme.hpp"

#include <string>
#include <vector>

struct SDL_Window;

namespace retcomm::studio {

// Build tab: the Toolchain section for `model.selected_root()`.
void draw_n64_toolchain_build(StudioModel& model, const Theme& th, SDL_Window* window,
                              float label_w);

// New Project: the same rows, editing the scaffold's tools (and, on request,
// the Studio default).
void draw_n64_toolchain_new_project(StudioModel& model, const Theme& th, SDL_Window* window,
                                    float label_w);

// `--tool KEY=PATH` for every non-empty New Project row, for the new-project
// CLI call. Empty rows are left to the Studio default, then to discovery.
std::vector<std::string> n64_new_project_tool_args();

} // namespace retcomm::studio
