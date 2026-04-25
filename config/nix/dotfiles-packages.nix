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
