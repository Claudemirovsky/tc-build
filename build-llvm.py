#!/usr/bin/env python3
# pylint: disable=invalid-name

from argparse import ArgumentParser, RawTextHelpFormatter
from pathlib import Path
import textwrap
import time

from tc_build.llvm_build_stages import LLVMStages
import tc_build.utils


# This is a known good revision of LLVM for building the kernel
GOOD_REVISION = '2ab9233f4f393c240c37ef092de09d907fe5c890'

# The version of the Linux kernel that the script downloads if necessary
DEFAULT_KERNEL_FOR_PGO = [6, 11, 0]

parser = ArgumentParser(formatter_class=RawTextHelpFormatter)
clone_options = parser.add_mutually_exclusive_group()
opt_options = parser.add_mutually_exclusive_group()

parser.add_argument(
    '--assertions',
    help=textwrap.dedent('''\
                    In a release configuration, assertions are not enabled. Assertions can help catch
                    issues when compiling but it will increase compile times by 15-20%%.

                    '''),
    action='store_true',
)
parser.add_argument(
    '-b',
    '--build-folder',
    help=textwrap.dedent('''\
                    By default, the script will create a "build/llvm" folder in the same folder as this
                    script and build each requested stage within that containing folder. To change the
                    location of the containing build folder, pass it to this parameter. This can be either
                    an absolute or relative path.

                    '''),
    type=str,
)
parser.add_argument(
    '--build-targets',
    default=['all'],
    help=textwrap.dedent('''\
                    By default, the 'all' target is used as the build target for the final stage. With
                    this option, targets such as 'distribution' could be used to generate a slimmer
                    toolchain or targets such as 'clang' or 'llvm-ar' could be used to just test building
                    individual tools for a bisect.

                    NOTE: This only applies to the final stage build to avoid complicating tc-build internals.
                    '''),
    nargs='+',
)
parser.add_argument(
    '--bolt',
    help=textwrap.dedent('''\
                    Optimize the final clang binary with BOLT (Binary Optimization and Layout Tool), which can
                    often improve compile time performance by 5-7%% on average.

                    This is similar to Profile Guided Optimization (PGO) but it happens against the final
                    binary that is built. The script will:

                    1. Figure out if perf can be used with branch sampling. You can test this ahead of time by
                       running:

                       $ perf record --branch-filter any,u --event cycles:u --output /dev/null -- sleep 1

                    2. If perf cannot be used, the clang binary will be instrumented by llvm-bolt, which will
                       result in a much slower clang binary.

                       NOTE #1: When this instrumentation is combined with a build of LLVM that has already
                                been PGO'd (i.e., the '--pgo' flag) without LLVM's internal assertions (i.e.,
                                no '--assertions' flag), there might be a crash when attempting to run the
                                instrumented clang:
                                https://github.com/llvm/llvm-project/issues/55004
                                To avoid this, pass '--assertions' with '--bolt --pgo'.

                       NOTE #2: BOLT's instrumentation might not be compatible with architectures other than
                                x86_64 and build-llvm.py's implementation has only been validated on x86_64
                                machines:
                                https://github.com/llvm/llvm-project/issues/55005
                                BOLT itself only appears to support AArch64 and x86_64 as of LLVM commit
                                a0b8ab1ba3165d468792cf0032fce274c7d624e1.

                    3. A kernel will be built and profiled. This will either be the host architecture's
                       defconfig or the first target's defconfig if '--targets' is specified without support
                       for the host architecture. The profiling data will be quite large, so it is imperative
                       that you have ample disk space and memory when attempting to do this. With instrumentation,
                       a profile will be generated for each invocation (PID) of clang, so this data could easily
                       be a couple hundred gigabytes large.

                    4. The clang binary will be optimized with BOLT using the profile generated above. This can
                       take some time.

                       NOTE #3: Versions of BOLT without commit 7d7771f34d14 ("[BOLT] Compact legacy profiles")
                                will use significantly more memory during this stage if instrumentation is used
                                because the merged profile is not as slim as it could be. Either upgrade to a
                                version of LLVM that contains that change or pick it yourself, switch to perf if
                                your machine supports it, upgrade the amount of memory you have (if possible),
                                or run build-llvm.py without '--bolt'.

                    '''),
    action='store_true',
)
opt_options.add_argument(
    '--build-stage1-only',
    help=textwrap.dedent('''\
                    By default, the script does a multi-stage build: it builds a more lightweight version of
                    LLVM first (stage 1) then uses that build to build the full toolchain (stage 2). This
                    is also known as bootstrapping.

                    This option avoids that, building the first stage as if it were the final stage. Note,
                    this option is more intended for quick testing and verification of issues and not regular
                    use. However, if your system is slow or can't handle 2+ stage builds, you may need this flag.

                         '''),
    action='store_true',
)
# yapf: disable
parser.add_argument('--build-type',
                    metavar='BUILD_TYPE',
                    help=textwrap.dedent('''\
                    By default, the script does a Release build; Debug may be useful for tracking down
                    particularly nasty bugs.

                    See https://llvm.org/docs/GettingStarted.html#compiling-the-llvm-suite-source-code for
                    more information.

                    '''),
                    type=str,
                    choices=['Release', 'Debug', 'RelWithDebInfo', 'MinSizeRel'])
# yapf: enable
parser.add_argument(
    '--check-targets',
    help=textwrap.dedent('''\
                    By default, no testing is run on the toolchain. If you would like to run unit/regression
                    tests, use this parameter to specify a list of check targets to run with ninja. Common
                    ones include check-llvm, check-clang, and check-lld.

                    The values passed to this parameter will be automatically concatenated with 'check-'.

                    Example: '--check-targets clang llvm' will make ninja invokve 'check-clang' and 'check-llvm'.

                    '''),
    nargs='+',
)
parser.add_argument(
    '-D',
    '--defines',
    help=textwrap.dedent('''\
                    Specify additional cmake values. These will be applied to all cmake invocations.

                    Example: -D LLVM_PARALLEL_COMPILE_JOBS=2 LLVM_PARALLEL_LINK_JOBS=2

                    See https://llvm.org/docs/CMake.html for various cmake values. Note that some of
                    the options to this script correspond to cmake values.

                    '''),
    nargs='+',
)
parser.add_argument(
    '-f',
    '--full-toolchain',
    help=textwrap.dedent('''\
                    By default, the script tunes LLVM for building the Linux kernel by disabling several
                    projects, targets, and configuration options, which speeds up build times but limits
                    how the toolchain could be used.

                    With this option, all projects and targets are enabled and the script tries to avoid
                    unnecessarily turning off configuration options. The '--projects' and '--targets' options
                    to the script can still be used to change the list of projects and targets. This is
                    useful when using the script to do upstream LLVM development or trying to use LLVM as a
                    system-wide toolchain.

                    '''),
    action='store_true',
)
parser.add_argument(
    '-i',
    '--install-folder',
    help=textwrap.dedent('''\
                    By default, the script will leave the toolchain in its build folder. To install it
                    outside the build folder for persistent use, pass the installation location that you
                    desire to this parameter. This can be either an absolute or relative path.

                    '''),
    type=str,
)
parser.add_argument(
    '--install-targets',
    help=textwrap.dedent('''\
                    By default, the script will just run the 'install' target to install the toolchain to
                    the desired prefix. To produce a slimmer toolchain, specify the desired targets to
                    install using this options.

                    The values passed to this parameter will be automatically prepended with 'install-'.

                    Example: '--install-targets clang lld' will make ninja invoke 'install-clang' and
                             'install-lld'.

                    '''),
    nargs='+',
)
parser.add_argument(
    '-l',
    '--llvm-folder',
    help=textwrap.dedent('''\
                    By default, the script will clone the llvm-project into the tc-build repo. If you have
                    another LLVM checkout that you would like to work out of, pass it to this parameter.
                    This can either be an absolute or relative path. Implies '--no-update'. When this
                    option is supplied, '--ref' and '--use-good-revison' do nothing, as the script does
                    not manipulate a repository it does not own.

                    '''),
    type=str,
)
parser.add_argument(
    '-L',
    '--linux-folder',
    help=textwrap.dedent('''\
                    If building with PGO, use this kernel source for building profiles instead of downloading
                    a tarball from kernel.org. This should be the full or relative path to a complete kernel
                    source directory, not a tarball or zip file.

                    '''),
    type=str,
)
parser.add_argument(
    '--lto',
    metavar='LTO_TYPE',
    help=textwrap.dedent('''\
                    Build the final compiler with either ThinLTO (thin) or full LTO (full), which can
                    often improve compile time performance by 3-5%% on average.

                    Only use full LTO if you have more than 64 GB of memory. ThinLTO uses way less memory,
                    compiles faster because it is fully multithreaded, and it has almost identical
                    performance (within 1%% usually) to full LTO. The compile time impact of ThinLTO is about
                    5x the speed of a '--build-stage1-only' build and 3.5x the speed of a default build. LTO
                    is much worse and is not worth considering unless you have a server available to build on.

                    This option should not be used with '--build-stage1-only' unless you know that your
                    host compiler and linker support it. See the two links below for more information.

                    https://llvm.org/docs/LinkTimeOptimization.html
                    https://clang.llvm.org/docs/ThinLTO.html

                    '''),
    type=str,
    choices=['thin', 'full'],
)
parser.add_argument(
    '-n',
    '--no-update',
    help=textwrap.dedent('''\
                    By default, the script always updates the LLVM repo before building. This prevents
                    that, which can be helpful during something like bisecting or manually managing the
                    repo to pin it to a particular revision.

                    '''),
    action='store_true',
)
parser.add_argument(
    '--no-ccache',
    help=textwrap.dedent('''\
                    By default, the script adds LLVM_CCACHE_BUILD to the cmake options so that ccache is
                    used for the stage one build. This helps speed up compiles but it is only useful for
                    stage one, which is built using the host compiler, which usually does not change,
                    resulting in more cache hits. Subsequent stages will be always completely clean builds
                    since ccache will have no hits due to using a new compiler and it will unnecessarily
                    fill up the cache with files that will never be called again due to changing compilers
                    on the next build. This option prevents ccache from being used even at stage one, which
                    could be useful for benchmarking clean builds.

                    '''),
    action='store_true',
)
parser.add_argument(
    '-p',
    '--projects',
    help=textwrap.dedent('''\
                    Currently, the script only enables the clang, compiler-rt, lld, and polly folders in LLVM.
                    If you would like to override this, you can use this parameter and supply a list that is
                    supported by LLVM_ENABLE_PROJECTS.

                    See step #5 here: https://llvm.org/docs/GettingStarted.html#getting-started-quickly-a-summary

                    Example: -p clang lld polly

                    '''),
    nargs='+',
)
opt_options.add_argument(
    '--pgo',
    metavar='PGO_BENCHMARK',
    help=textwrap.dedent('''\
                    Build the final compiler with Profile Guided Optimization, which can often improve compile
                    time performance by 15-20%% on average. The script will:

                    1. Build a small bootstrap compiler like usual (stage 1).

                    2. Build an instrumented compiler with that compiler (stage 2).

                    3. Run the specified benchmark(s).

                       kernel-defconfig, kernel-allmodconfig, kernel-allyesconfig:

                       Download and extract kernel source from kernel.org (unless '--linux-folder' is
                       specified) and build some kernels based on the requested config with the instrumented
                       compiler (based on the '--targets' option). If there is a build error with one of the
                       kernels, build-llvm.py will fail as well.

                       kernel-defconfig-slim, kernel-allmodconfig-slim, kernel-allyesconfig-slim:

                       Same as above but only one kernel will be built. If the host architecture is in the list
                       of targets, that architecture's requested config will be built; otherwise, the config of
                       the first architecture in '--targets' will be built. This will result in a less optimized
                       toolchain than the full variant above but it will result in less time spent profiling,
                       which means less build time overall. This might be worthwhile if you want to take advantage
                       of PGO on slower machines.

                       llvm:

                       The script will run the LLVM tests if they were requested via '--check-targets' then
                       build a full LLVM toolchain with the instrumented compiler.

                    4. Build a final compiler with the profile data generated from step 3 (stage 3).

                    Due to the nature of this process, '--build-stage1-only' cannot be used. There will be
                    three distinct LLVM build folders/compilers and several kernel builds done by default so
                    ensure that you have enough space on your disk to hold this (25GB should be enough) and the
                    time/patience to build three toolchains and kernels (will often take 5x the amount of time
                    as '--build-stage1-only' and 4x the amount of time as the default two-stage build that the
                    script does). When combined with '--lto', the compile time impact is about 9-10x of a one or
                    two stage builds.

                    See https://llvm.org/docs/HowToBuildWithPGO.html for more information.

                         '''),
    nargs='+',
    choices=[
        'kernel-defconfig',
        'kernel-allmodconfig',
        'kernel-allyesconfig',
        'kernel-defconfig-slim',
        'kernel-allmodconfig-slim',
        'kernel-allyesconfig-slim',
        'llvm',
    ],
)

parser.add_argument(
    '--cspgo',
    help="Enables Context-Sensitive PGO. Requires enabling normal PGO.",
    action="store_true",
)
parser.add_argument(
    '--quiet-cmake',
    help=textwrap.dedent('''\
                    By default, the script shows all output from cmake. When this option is enabled, the
                    invocations of cmake will only show warnings and errors.

                    '''),
    action='store_true',
)
parser.add_argument(
    '-r',
    '--ref',
    help=textwrap.dedent('''\
                    By default, the script builds the main branch (tip of tree) of LLVM. If you would
                    like to build an older branch, use this parameter. This may be helpful in tracking
                    down an older bug to properly bisect. This value is just passed along to 'git checkout'
                    so it can be a branch name, tag name, or hash (unless '--shallow-clone' is used, which
                    means a hash cannot be used because GitHub does not allow it). This will have no effect
                    if '--llvm-folder' is provided, as the script does not manipulate a repository that it
                    does not own.

                    '''),
    default='main',
    type=str,
)
clone_options.add_argument(
    '-s',
    '--shallow-clone',
    help=textwrap.dedent('''\
                    Only fetch the required objects and omit history when cloning the LLVM repo. This
                    option is only used for the initial clone, not subsequent fetches. This can break
                    the script's ability to automatically update the repo to newer revisions or branches
                    so be careful using this. This option is really designed for continuous integration
                    runs, where a one off clone is necessary. A better option is usually managing the repo
                    yourself:

                    https://github.com/ClangBuiltLinux/tc-build#build-llvmpy

                    A couple of notes:

                    1. This cannot be used with '--use-good-revision'.

                    2. When no '--branch' is specified, only main is fetched. To work with other branches,
                       a branch other than main needs to be specified when the repo is first cloned.

                           '''),
    action='store_true',
)
parser.add_argument(
    "-S",
    "--stage",
    help="Only run a single specific build stage",
    choices=[
        "bootstrap",
        "instrumentation",
        "profiling",
        "csinstrumentation",
        "csprofiling",
        "final",
    ],
)
parser.add_argument(
    '--show-build-commands',
    help=textwrap.dedent('''\
                    By default, the script only shows the output of the comands it is running. When this option
                    is enabled, the invocations of cmake, ninja, and make will be shown to help with
                    reproducing issues outside of the script.

                    '''),
    action='store_true',
)
parser.add_argument(
    '-t',
    '--targets',
    help=textwrap.dedent('''\
                    LLVM is multitargeted by default. Currently, this script only enables the arm32, aarch64,
                    bpf, mips, powerpc, riscv, s390, and x86 backends because that's what the Linux kernel is
                    currently concerned with. If you would like to override this, you can use this parameter
                    and supply a list of targets supported by LLVM_TARGETS_TO_BUILD:

                    https://llvm.org/docs/CMake.html#llvm-specific-variables

                    Example: -t AArch64 ARM X86

                    '''),
    nargs='+',
)
clone_options.add_argument(
    '--use-good-revision',
    help=textwrap.dedent('''\
                    By default, the script updates LLVM to the latest tip of tree revision, which may at times be
                    broken or not work right. With this option, it will checkout a known good revision of LLVM
                    that builds and works properly. If you use this option often, please remember to update the
                    script as the known good revision will change.

                    NOTE: This option cannot be used with '--shallow-clone'.

                           '''),
    action='store_const',
    const=GOOD_REVISION,
    dest='ref',
)
parser.add_argument(
    '--vendor-string',
    help=textwrap.dedent('''\
                    Add this value to the clang and ld.lld version string (like "Apple clang version..."
                    or "Android clang version..."). Useful when reverting or applying patches on top
                    of upstream clang to differentiate a toolchain built with this script from
                    upstream clang or to distinguish a toolchain built with this script from the
                    system's clang. Defaults to ClangBuiltLinux, can be set to an empty string to
                    override this and have no vendor in the version string.

                    '''),
    type=str,
    default='ClangBuiltLinux',
)
args = parser.parse_args()


# Start tracking time that the script takes
script_start = time.time()

# Folder validation
tc_build_folder = Path(__file__).resolve().parent
src_folder = Path(tc_build_folder, 'src')

if args.build_folder:
    build_folder = Path(args.build_folder).resolve()
else:
    build_folder = Path(tc_build_folder, 'build/llvm')

stages = LLVMStages(args, src_folder, build_folder, DEFAULT_KERNEL_FOR_PGO)

# Build bootstrap compiler if user did not request a single stage build
if (use_bootstrap := not args.build_stage1_only) and (not args.stage or args.stage == "bootstrap"):
    stages.bootstrap()

stages.update_defines()

if args.pgo:
    if not args.stage or args.stage == "instrumentation":
        stages.instrumentation()
    if not args.stage or args.stage == "profiling":
        stages.profiling()

    if args.cspgo:
        if not args.stage or args.stage == "csinstrumentation":
            stages.instrumentation(cspgo=True)
        if not args.stage or args.stage == "csprofiling":
            stages.profiling(cspgo=True)
if not args.stage or args.stage == "final":
    # Final build
    if args.pgo and not stages.instrumented:
        stages.instrumented = stages.setup_instrumentation(cspgo=args.cspgo)
    stages.final_step()

print(f"Script duration: {tc_build.utils.get_duration(script_start)}")
