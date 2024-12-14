import platform
import time
from argparse import Namespace
from pathlib import Path
import sys
import tc_build.utils
from tc_build.kernel import KernelBuilder, LinuxSourceManager, LLVMKernelBuilder
from tc_build.llvm import (
    LLVMBootstrapBuilder,
    LLVMBuilder,
    LLVMCSPGOInstrumentedBuilder,
    LLVMInstrumentedBuilder,
    LLVMSlimBuilder,
    LLVMSlimCSPGOInstrumentedBuilder,
    LLVMSlimInstrumentedBuilder,
    LLVMSourceManager,
)
from tc_build.tools import HostTools, StageTools


def build_stage(title):
    def wrapper1(func):
        def wrapper2(self, *args, **kwargs):
            tc_build.utils.print_header(title)
            func(self, *args, **kwargs)
            if self.args.stage:
                tc_build.utils.print_info(title + ' done.')
                sys.exit(0)

        return wrapper2

    return wrapper1


class LLVMStages:
    def __init__(
        self, args: Namespace, src_folder: Path, build_folder: Path, kernel_version: list[int]
    ):
        self.args = args
        self.lsm = None
        self.instrumented: LLVMInstrumentedBuilder | None = None
        self.build_folder = build_folder
        self.bootstrap_dir = build_folder / "bootstrap"
        self.instrumentation_dir = build_folder / "instrumentation"
        self.cspgo_instrumentation_dir = build_folder / "cspgo_instrumentation"
        self.profiling_dir = build_folder / "profiling"
        self.cspgo_profiling_dir = build_folder / "cspgo_profiling"
        self.final_dir = build_folder / "final"
        # Validate and prepare Linux source if doing BOLT or PGO with kernel benchmarks
        # Check for issues early, as these technologies are time consuming, so a user
        # might step away from the build once it looks like it has started
        if args.bolt or (args.pgo and [x for x in args.pgo if 'kernel' in x]):
            lsm = LinuxSourceManager()
            if args.linux_folder:
                if not (linux_folder := Path(args.linux_folder).resolve()).exists():
                    raise RuntimeError(
                        f"Provided Linux folder ('{args.linux_folder}') does not exist?"
                    )
                if not Path(linux_folder, 'Makefile').exists():
                    raise RuntimeError(
                        f"Provided Linux folder ('{args.linux_folder}') does not appear to be a Linux kernel tree?"
                    )

                lsm.location = linux_folder

                # The kernel builder used by PGO below is written with a minimum
                # version in mind. If the user supplied their own Linux source, make
                # sure it is recent enough that the kernel builder will work.
                if (linux_version := lsm.get_version()) < KernelBuilder.MINIMUM_SUPPORTED_VERSION:
                    found_version = '.'.join(map(str, linux_version))
                    minimum_version = '.'.join(map(str, KernelBuilder.MINIMUM_SUPPORTED_VERSION))
                    raise RuntimeError(
                        f"Supplied kernel source version ('{found_version}') is older than the minimum required version ('{minimum_version}'), provide a newer version!"
                    )
            else:
                # Turns (6, 2, 0) into 6.2 and (6, 2, 1) into 6.2.1 to follow tarball names
                ver_str = '.'.join(str(x) for x in kernel_version if x)
                lsm.location = Path(src_folder, f"linux-{ver_str}")
                lsm.patches = list(src_folder.glob('*.patch'))

                lsm.tarball.base_download_url = 'https://cdn.kernel.org/pub/linux/kernel/v6.x'
                lsm.tarball.local_location = lsm.location.with_name(f"{lsm.location.name}.tar.xz")
                lsm.tarball.remote_checksum_name = 'sha256sums.asc'

                tc_build.utils.print_header('Preparing Linux source for profiling runs')
                lsm.prepare()
            self.lsm = lsm

        # Validate and configure LLVM source
        if args.llvm_folder:
            if not (llvm_folder := Path(args.llvm_folder).resolve()).exists():
                raise RuntimeError(f"Provided LLVM folder ('{args.llvm_folder}') does not exist?")
        else:
            llvm_folder = Path(src_folder, 'llvm-project')

        self.llvm_folder = llvm_folder
        self.llvm_source = LLVMSourceManager(llvm_folder)
        self.llvm_source.download(args.ref, args.shallow_clone)
        if not (args.llvm_folder or args.no_update):
            self.llvm_source.update(args.ref)

        # Get host tools
        tc_build.utils.print_header('Checking CC and LD')

        self.host_tools = HostTools()
        self.host_tools.show_compiler_linker()

        # '--full-toolchain' affects all stages aside from the bootstrap stage so cache
        # the class for all future initializations.
        self.def_llvm_builder_cls = LLVMBuilder if args.full_toolchain else LLVMSlimBuilder

        # Instantiate final builder to validate user supplied targets ahead of time, so
        # that the user can correct the issue sooner rather than later.
        self.final = self.def_llvm_builder_cls()
        self.final.folders.source = llvm_folder
        if args.targets:
            self.final.targets = args.targets
            self.final.validate_targets()
        else:
            self.final.targets = (
                ['all'] if args.full_toolchain else self.llvm_source.default_targets()
            )

        # Configure projects
        if args.projects:
            self.final.projects = args.projects
        elif args.full_toolchain:
            self.final.projects = ['all']
        else:
            self.final.projects = self.llvm_source.default_projects()

        # Warn the user of certain issues with BOLT and instrumentation
        if args.bolt and not self.final.can_use_perf():
            warned = False
            has_4f158995b9cddae = Path(llvm_folder, 'bolt/lib/Passes/ValidateMemRefs.cpp').exists()
            if args.pgo and not args.assertions and not has_4f158995b9cddae:
                tc_build.utils.print_warning(
                    'Using BOLT in instrumentation mode with PGO and no assertions might result in a binary that crashes:'
                )
                tc_build.utils.print_warning('https://github.com/llvm/llvm-project/issues/55004')
                tc_build.utils.print_warning(
                    "Consider adding '--assertions' if there are any failures during the BOLT stage."
                )
                warned = True
            if platform.machine() != 'x86_64':
                tc_build.utils.print_warning(
                    'Using BOLT in instrumentation mode may not work on non-x86_64 machines:'
                )
                tc_build.utils.print_warning('https://github.com/llvm/llvm-project/issues/55005')
                tc_build.utils.print_warning(
                    "Consider dropping '--bolt' if there are any failures during the BOLT stage."
                )
                warned = True
            if warned:
                tc_build.utils.print_warning('Continuing in 5 seconds, hit Ctrl-C to cancel...')
                time.sleep(5)

        # Figure out unconditional cmake defines from input
        self.common_cmake_defines = {}
        if args.assertions:
            self.common_cmake_defines['LLVM_ENABLE_ASSERTIONS'] = 'ON'
        if args.vendor_string:
            self.common_cmake_defines['CLANG_VENDOR'] = args.vendor_string
            self.common_cmake_defines['LLD_VENDOR'] = args.vendor_string
        if args.defines:
            defines = dict(define.split('=', 1) for define in args.defines)
            self.common_cmake_defines.update(defines)

    def configure_llvm_builder(
        self, instance: LLVMBuilder, builddir: Path, tools: Path | None = None
    ):
        instance.show_commands = self.args.show_build_commands
        instance.quiet_cmake = self.args.quiet_cmake
        instance.folders.source = self.llvm_folder
        instance.folders.build = builddir
        instance.cmake_defines.update(self.common_cmake_defines)
        if tools is not None:
            instance.tools = StageTools(tools / 'bin')
            instance.targets = self.final.targets
            instance.projects = self.final.projects
        else:
            instance.tools = self.host_tools

    @build_stage("Building LLVM (bootstrap)")
    def bootstrap(self):
        bootstrap = LLVMBootstrapBuilder()
        bootstrap.build_targets = ['distribution']
        bootstrap.ccache = not self.args.no_ccache
        self.configure_llvm_builder(bootstrap, self.bootstrap_dir)
        if self.args.bolt:
            bootstrap.projects.append('bolt')
        if self.args.pgo:
            bootstrap.projects.append('compiler-rt')

        bootstrap.check_dependencies()
        bootstrap.configure()
        bootstrap.build()

    def update_defines(self):
        # If the user did not specify CMAKE_C_FLAGS or CMAKE_CXX_FLAGS, add them as empty
        # to paste stage 2 to ensure there are no environment issues (since CFLAGS and CXXFLAGS
        # are taken into account by cmake)
        c_flag_defines = ['CMAKE_C_FLAGS', 'CMAKE_CXX_FLAGS']
        for define in c_flag_defines:
            if define not in self.common_cmake_defines:
                self.common_cmake_defines[define] = ''
        # The user's build type should be taken into account past the bootstrap compiler
        if self.args.build_type:
            self.common_cmake_defines['CMAKE_BUILD_TYPE'] = self.args.build_type

    def setup_instrumentation(self, cspgo: bool = False) -> LLVMInstrumentedBuilder:
        choices = (
            (LLVMSlimInstrumentedBuilder, LLVMInstrumentedBuilder),
            (LLVMSlimCSPGOInstrumentedBuilder, LLVMCSPGOInstrumentedBuilder),
        )
        instrumented = choices[cspgo][self.args.full_toolchain]()

        instrumented.build_targets = ['all' if self.args.full_toolchain else 'distribution']
        # We run the tests on the instrumented stage if the LLVM benchmark was enabled
        instrumented.check_targets = self.args.check_targets if 'llvm' in self.args.pgo else []
        instrumentation_dir = self.cspgo_instrumentation_dir if cspgo else self.instrumentation_dir
        self.configure_llvm_builder(instrumented, instrumentation_dir, self.bootstrap_dir)

        return instrumented

    @build_stage("Building LLVM (instrumented)")
    def instrumentation(self, cspgo: bool = False):
        self.instrumented = self.setup_instrumentation(cspgo=cspgo)
        self.instrumented.configure()
        self.instrumented.build()

    @build_stage("Generating PGO profiles")
    def profiling(self, cspgo: bool = False):
        instrumented = (
            self.instrumented if self.instrumented else self.setup_instrumentation(cspgo=cspgo)
        )
        pgo_builders = []
        items = (
            (self.profiling_dir, self.instrumentation_dir),
            (self.cspgo_profiling_dir, self.cspgo_instrumentation_dir),
        )
        profiling_dir, instrumentation_dir = items[cspgo]
        if 'llvm' in self.args.pgo:
            llvm_builder = self.def_llvm_builder_cls()
            self.configure_llvm_builder(llvm_builder, profiling_dir, instrumentation_dir)
            # clang-tblgen and llvm-tblgen may not be available from the
            # instrumented folder if the user did not pass '--full-toolchain', as
            # only the tools included in the distribution will be available. In
            # that case, use the bootstrap versions, which should not matter much
            # for profiling sake.
            if not self.args.full_toolchain and llvm_builder.tools is not None:
                llvm_builder.tools.clang_tblgen = self.bootstrap_dir / 'bin/clang-tblgen'
                llvm_builder.tools.llvm_tblgen = self.bootstrap_dir / 'bin/llvm-tblgen'
            pgo_builders.append(llvm_builder)

        # If the user specified both a full and slim build of the same type, remove
        # the full build and warn them.
        pgo_targets = [s.replace('kernel-', '') for s in self.args.pgo if 'kernel-' in s]
        for pgo_target in pgo_targets:
            if 'slim' not in pgo_target:
                continue
            config_target = pgo_target.split('-')[0]
            if config_target in pgo_targets:
                tc_build.utils.print_warning(
                    f"Both full and slim were specified for {config_target}, ignoring full..."
                )
                pgo_targets.remove(config_target)

        if pgo_targets:
            kernel_builder = LLVMKernelBuilder()
            kernel_builder.folders.build = Path(self.build_folder, 'linux')
            kernel_builder.folders.source = self.lsm.location if self.lsm else None
            kernel_builder.toolchain_prefix = instrumentation_dir
            for item in pgo_targets:
                pgo_target = item.split('-')

                config_target = pgo_target[0]
                # For BOLT or "slim" PGO, we limit the number of kernels we build for
                # each mode:
                #
                # When using perf, building too many kernels will generate a gigantic
                # perf profile. perf2bolt calls 'perf script', which will load the
                # entire profile into memory, which could cause OOM for most machines
                # and long processing times for the ones that can handle it for little
                # extra gain.
                #
                # With BOLT instrumentation, we generate one profile file for each
                # invocation of clang (PID) to avoid profiling just the driver, so
                # building multiple kernels will generate a few hundred gigabytes of
                # fdata files.
                #
                # Just do a native build if the host target is in the list of targets
                # or the first target if not.
                if len(pgo_target) == 2:  # slim
                    if instrumented.host_target_is_enabled():
                        llvm_targets = [instrumented.host_target()]
                    else:
                        llvm_targets = self.final.targets[0:1]
                # full
                elif 'all' in self.final.targets:
                    llvm_targets = self.llvm_source.default_targets()
                else:
                    llvm_targets = self.final.targets

                kernel_builder.matrix[config_target] = llvm_targets

            pgo_builders.append(kernel_builder)

        for pgo_builder in pgo_builders:
            if hasattr(pgo_builder, 'configure') and callable(pgo_builder.configure):
                tc_build.utils.print_info('Building LLVM for profiling...')
                pgo_builder.configure()
            pgo_builder.build()

        instrumented.generate_profdata()

    @build_stage("Building LLVM (final)")
    def final_step(self):
        final = self.final
        final.build_targets = self.args.build_targets
        final.check_targets = self.args.check_targets
        final.folders.install = (
            Path(self.args.install_folder).resolve() if self.args.install_folder else None
        )
        final.install_targets = self.args.install_targets
        self.configure_llvm_builder(final, self.final_dir, self.bootstrap_dir)

        if self.args.lto:
            final.cmake_defines['LLVM_ENABLE_LTO'] = self.args.lto.capitalize()
        if (
            self.args.pgo
            and self.instrumented is not None
            and self.instrumented.folders.build is not None
        ):
            final.cmake_defines['LLVM_PROFDATA_FILE'] = Path(
                self.instrumented.profiles_output_path, self.instrumented.final_name
            )

        if self.args.build_stage1_only:
            # If we skipped bootstrapping, we need to check the dependencies now
            # and pass along certain user options
            final.check_dependencies()
            final.ccache = not self.args.no_ccache
            final.tools = self.host_tools

            # If the user requested BOLT but did not specify it in their projects nor
            # bootstrapped, we need to enable it to get the tools we need.
            if self.args.bolt:
                if not ('all' in final.projects or 'bolt' in self.final.projects):
                    final.projects.append('bolt')
                final.tools.llvm_bolt = self.final_dir / 'bin/llvm-bolt'
                final.tools.merge_fdata = self.final_dir / 'bin/merge-fdata'
                final.tools.perf2bolt = self.final_dir / 'bin/perf2bolt'

        if self.args.bolt:
            final.bolt = True
            final.bolt_builder = LLVMKernelBuilder()
            final.bolt_builder.folders.build = Path(self.build_folder, 'linux')
            final.bolt_builder.folders.source = self.lsm.location if self.lsm else None
            if final.host_target_is_enabled():
                llvm_targets = [final.host_target()]
            else:
                llvm_targets = final.targets[0:1]
            final.bolt_builder.matrix['defconfig'] = llvm_targets

        final.configure()
        final.build()
        final.show_install_info()
