#!/usr/bin/env python3
"""Generate the standalone Xcode app project, using only Python's standard library."""
from pathlib import Path
import hashlib
ROOT = Path(__file__).resolve().parents[1]
def uid(name):
    return hashlib.sha256(name.encode()).hexdigest()[:24].upper()
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
objects = []
def obj(name, body):
    objects.append(f'{uid(name)} = {{ {body} }};')
    return uid(name)
source_refs, builds = [], []
for path in sorted([*ROOT.glob('Sources/WispCore/*.swift'), *ROOT.glob('App/*.swift')]):
    relative = path.relative_to(ROOT).as_posix()
    ref = obj('ref:' + relative, f'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {quote(relative)}; sourceTree = "<group>";')
    source_refs.append(ref)
    builds.append(obj('build:' + relative, f'isa = PBXBuildFile; fileRef = {ref};'))
product = obj('product', 'isa = PBXFileReference; explicitFileType = wrapper.application; path = WispPhone.app; sourceTree = BUILT_PRODUCTS_DIR;')
products = obj('products', f'isa = PBXGroup; children = ({product},); name = Products; sourceTree = "<group>";')
main = obj('group', f'isa = PBXGroup; children = ({",".join(source_refs + [products])},); sourceTree = "<group>";')
sources = obj('sources', f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({",".join(builds)},); runOnlyForDeploymentPostprocessing = 0;')
frameworks = obj('frameworks', 'isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
resources = obj('resources', 'isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;')
project_configs, target_configs = [], []
for config in ['Debug', 'Release']:
    project_configs.append(obj('project:' + config, f'isa = XCBuildConfiguration; name = {config}; buildSettings = {{ CLANG_ENABLE_MODULES = YES; SDKROOT = iphoneos; IPHONEOS_DEPLOYMENT_TARGET = 17.0; }};'))
    target_configs.append(obj('target:' + config, f'''isa = XCBuildConfiguration; name = {config}; buildSettings = {{
        PRODUCT_NAME = WispPhone; PRODUCT_BUNDLE_IDENTIFIER = app.wisp.iphone.prototype;
        SWIFT_VERSION = 6.0; SWIFT_STRICT_CONCURRENCY = complete;
        INFOPLIST_FILE = App/Info.plist; GENERATE_INFOPLIST_FILE = NO;
        TARGETED_DEVICE_FAMILY = 1; CODE_SIGN_STYLE = Automatic;
        CURRENT_PROJECT_VERSION = 1; MARKETING_VERSION = 0.1.0;
        SWIFT_OPTIMIZATION_LEVEL = {quote('-Onone' if config == 'Debug' else '-O')};
    }};'''))
project_list = obj('project-configs', f'isa = XCConfigurationList; buildConfigurations = ({",".join(project_configs)},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
target_list = obj('target-configs', f'isa = XCConfigurationList; buildConfigurations = ({",".join(target_configs)},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
target = obj('target', f'isa = PBXNativeTarget; buildConfigurationList = {target_list}; buildPhases = ({sources},{frameworks},{resources},); buildRules = (); dependencies = (); name = WispPhone; productName = WispPhone; productReference = {product}; productType = "com.apple.product-type.application";')
project = obj('project', f'isa = PBXProject; attributes = {{ LastUpgradeCheck = 2700; }}; buildConfigurationList = {project_list}; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en,Base,); mainGroup = {main}; productRefGroup = {products}; projectDirPath = ""; projectRoot = ""; targets = ({target},);')
destination = ROOT / 'WispPhone.xcodeproj'
destination.mkdir(exist_ok=True)
(destination / 'project.pbxproj').write_text('// !$*UTF8*$!\n{ archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n' + '\n'.join(objects) + f'\n}}; rootObject = {project}; }}\n')
schemes = destination / 'xcshareddata/xcschemes'
schemes.mkdir(parents=True, exist_ok=True)
reference = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target}" BuildableName="WispPhone.app" BlueprintName="WispPhone" ReferencedContainer="container:WispPhone.xcodeproj"/>'
(schemes / 'WispPhone.xcscheme').write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="2700" version="1.3">
<BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{reference}</BuildActionEntry></BuildActionEntries></BuildAction>
<LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></LaunchAction>
<ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></ProfileAction>
<AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>
''')
