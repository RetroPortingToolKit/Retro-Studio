#pragma once

// The Bulk Recomp tab — probe, scaffold, generate and build many game images
// into one output folder, for testing the pipeline across a set of dumps.
//
// The orchestration is NOT here. It is `project_studio bulk-recomp`
// (bulkrecomp.py), which runs each stage as the same CLI subcommand the New
// Project and Build tabs run for one image, and which works headless. This tab
// picks the images, starts that one process, and draws what it reports in
// <out>/Log Output/bulk_status.json. Stage output goes to per-stage log files
// in that folder rather than to the Activity log: interleaved output from
// several builds at once is unreadable.
//
// The run holds none of Studio's job slots. It touches only its own output
// folder, and a batch can run for hours; holding the project slot for that
// long would grey out every other tab for no reason.

#include "studio/studio_model.hpp"

struct SDL_Window;

namespace retcomm::studio {

struct Theme;

void draw_bulk_recomp(StudioModel& model, const Theme& th, SDL_Window* window);

// Ask a running batch to stop (called on exit, so closing Studio does not
// leave a batch compiling in the background with nobody watching).
void bulk_recomp_shutdown();

} // namespace retcomm::studio
