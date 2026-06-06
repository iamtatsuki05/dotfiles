{ pkgs }:

let
  inherit (pkgs) lib stdenv;

  darwinOnly = with lib.platforms; darwin;
in
{
  displayplacer = stdenv.mkDerivation rec {
    pname = "displayplacer";
    version = "1.4.0";

    src = pkgs.fetchurl {
      url = "https://github.com/jakehilborn/displayplacer/archive/v${version}.tar.gz";
      sha256 = "54b239359dbf9dc9b3a25e41a372eafb1de6c3131fe7fed37da53da77189b600";
    };

    buildPhase = ''
      runHook preBuild
      make -C src
      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall
      install -Dm755 src/displayplacer "$out/bin/displayplacer"
      runHook postInstall
    '';

    meta = {
      description = "macOS command line utility to configure multi-display resolutions and arrangements";
      homepage = "https://github.com/jakehilborn/displayplacer";
      license = lib.licenses.mit;
      mainProgram = "displayplacer";
      platforms = darwinOnly;
    };
  };

  e2b = pkgs.buildNpmPackage rec {
    pname = "e2b";
    version = "2.9.0";

    src = pkgs.fetchurl {
      url = "https://registry.npmjs.org/@e2b/cli/-/cli-${version}.tgz";
      sha256 = "74afdd3a0f93d263cd752564a3af202bc5086eed248facccd5b72b90317ecdcb";
    };

    postPatch = ''
      cp ${./packages/e2b/package-lock.json} package-lock.json
    '';

    npmDepsHash = "sha256-FoSEF8VThQ4unfMF+rNkqTSiUeplBS6GnAY9HKufkYY=";
    dontNpmBuild = true;

    meta = {
      description = "CLI to manage E2B sandboxes and templates";
      homepage = "https://e2b.dev";
      license = lib.licenses.mit;
      mainProgram = "e2b";
      platforms = lib.platforms.all;
    };
  };

  hermes-desktop =
    let
      version = "0.16.0";
      build = "b91aade17683";
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "hermes-desktop";
      inherit version;

      src = pkgs.fetchurl {
        name = "Hermes-Setup-${build}.dmg";
        url = "https://hermes-assets.nousresearch.com/Hermes-Setup.dmg?build=${build}";
        hash = "sha256-th4Efv4wWfrxxV/sMlLmYfLSqZOno+6/XMapqlwXkPU=";
      };

      dontUnpack = true;
      dontBuild = true;
      dontFixup = true;

      installPhase = ''
        runHook preInstall
        mkdir -p "$out/Applications"
        mount="$(mktemp -d)"
        cleanup() {
          /usr/bin/hdiutil detach "$mount" >/dev/null 2>&1 || true
        }
        trap cleanup EXIT
        /usr/bin/hdiutil attach -nobrowse -readonly -mountpoint "$mount" "$src"
        cp -R "$mount/Hermes.app" "$out/Applications/"
        cleanup
        trap - EXIT
        runHook postInstall
      '';

      meta = {
        description = "Desktop companion for Hermes Agent";
        homepage = "https://hermes-agent.nousresearch.com/desktop";
        license = lib.licenses.mit;
        platforms = darwinOnly;
        sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
      };
    };

  mactop = pkgs.mactop.overrideAttrs (old: {
    doCheck = false;
    doInstallCheck = false;
    nativeInstallCheckInputs = [ ];
    meta = old.meta // {
      platforms = [ "aarch64-darwin" ];
    };
  });

  magika-cli = pkgs.magika-cli.overrideAttrs (old: {
    doCheck = false;
    doInstallCheck = false;
    nativeInstallCheckInputs = [ ];
    postInstall = (old.postInstall or "") + lib.optionalString stdenv.hostPlatform.isDarwin ''
      install_name_tool -add_rpath ${lib.getLib pkgs.onnxruntime}/lib "$out/bin/magika"
    '';
    meta = old.meta // {
      broken = false;
    };
  });

  waza =
    let
      version = "0.31.0";
      asset =
        if stdenv.hostPlatform.isDarwin && stdenv.hostPlatform.isAarch64 then
          {
            name = "waza-darwin-arm64";
            hash = "sha256-gMMK9rUdePY5UMhGhFvFJeeHixzfaJV7r91Jh0/6FfE=";
          }
        else if stdenv.hostPlatform.isDarwin && stdenv.hostPlatform.isx86_64 then
          {
            name = "waza-darwin-amd64";
            hash = "sha256-1bixv2g1gULHOBeXgbRKhecJ5KHOzPNKt8E+ykQn3S4=";
          }
        else if stdenv.hostPlatform.isLinux && stdenv.hostPlatform.isAarch64 then
          {
            name = "waza-linux-arm64";
            hash = "sha256-oooOfWSh1IK9PMdAX42phZv83o279skUT/BRpcSFlfE=";
          }
        else if stdenv.hostPlatform.isLinux && stdenv.hostPlatform.isx86_64 then
          {
            name = "waza-linux-amd64";
            hash = "sha256-vD2wYJcE0WPpDPexF/MbIUgsATciAGaiCHFNwxKV594=";
          }
        else
          throw "unsupported platform for waza: ${stdenv.hostPlatform.system}";
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "waza";
      inherit version;

      src = pkgs.fetchurl {
        url = "https://github.com/microsoft/waza/releases/download/v${version}/${asset.name}";
        inherit (asset) hash;
      };

      dontUnpack = true;

      installPhase = ''
        runHook preInstall
        install -Dm755 "$src" "$out/bin/waza"
        runHook postInstall
      '';

      meta = {
        description = "CLI and framework for evaluating AI agent skills";
        homepage = "https://github.com/microsoft/waza";
        license = lib.licenses.mit;
        mainProgram = "waza";
        platforms = [
          "aarch64-darwin"
          "x86_64-darwin"
          "aarch64-linux"
          "x86_64-linux"
        ];
      };
    };

  mise =
    (pkgs.mise.override {
      direnv = pkgs.direnv.overrideAttrs (_: {
        doCheck = false;
      });
    }).overrideAttrs (_: {
      doCheck = false;
      nativeCheckInputs = [ ];
    });

  z = pkgs.stdenvNoCC.mkDerivation rec {
    pname = "z";
    version = "1.12";

    src = pkgs.fetchurl {
      url = "https://github.com/rupa/z/archive/refs/tags/v${version}.tar.gz";
      sha256 = "7d8695f2f5af6805f0db231e6ed571899b8b375936a8bfca81a522b7082b574e";
    };

    dontBuild = true;

    installPhase = ''
      runHook preInstall
      install -Dm644 z.sh "$out/etc/profile.d/z.sh"
      install -Dm644 z.1 "$out/share/man/man1/z.1"
      runHook postInstall
    '';

    meta = {
      description = "Tracks most-used directories to make cd smarter";
      homepage = "https://github.com/rupa/z";
      license = lib.licenses.wtfpl;
      platforms = lib.platforms.all;
    };
  };
}
