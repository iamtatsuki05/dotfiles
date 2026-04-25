{
  description = "Cross-platform dotfiles managed by Nix, nix-darwin, and Home Manager";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    nix-darwin = {
      url = "github:nix-darwin/nix-darwin";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = inputs@{ self, nixpkgs, nix-darwin, home-manager, ... }:
    let
      lib = nixpkgs.lib;
      username = "tatsuki";
      homeManagerBackupExtension = "before-nix-darwin";

      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];

      darwinSystems = [
        "aarch64-darwin"
        "x86_64-darwin"
      ];

      forAllSystems = f:
        lib.genAttrs systems (system: f system);

      mkPkgs = system:
        import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };

      homeDirectoryFor = system:
        if lib.hasSuffix "-darwin" system then
          "/Users/${username}"
        else
          "/home/${username}";

      mkHomeConfiguration = { system, profile, enableGuiApps ? profile == "full" }:
        let
          pkgs = mkPkgs system;
        in
        home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          extraSpecialArgs = {
            inherit inputs profile enableGuiApps system username;
            homeDirectory = homeDirectoryFor system;
          };
          modules = [
            ./config/nix/home-manager
          ];
        };

      mkDarwinConfiguration = { system, profile, enableGuiApps ? profile == "full" }:
        let
          homeDirectory = homeDirectoryFor system;
        in
        nix-darwin.lib.darwinSystem {
          inherit system;
          specialArgs = {
            inherit inputs profile enableGuiApps system username homeDirectory;
          };
          modules = [
            ./config/nix/darwin
            home-manager.darwinModules.home-manager
            {
              home-manager.useGlobalPkgs = true;
              home-manager.useUserPackages = true;
              home-manager.backupFileExtension = homeManagerBackupExtension;
              home-manager.extraSpecialArgs = {
                inherit inputs profile enableGuiApps system username homeDirectory;
              };
              home-manager.users.${username} = import ./config/nix/home-manager;
            }
          ];
        };

      packageSetFor = system:
        let
          pkgs = mkPkgs system;
          cliPackages = import ./config/nix/packages.nix { inherit pkgs; };
          guiPackages = import ./config/nix/gui-packages.nix { inherit pkgs; };
        in
        {
          dotfiles-cli-packages = pkgs.buildEnv {
            name = "dotfiles-cli-packages";
            paths = cliPackages;
          };

          dotfiles-full-packages = pkgs.buildEnv {
            name = "dotfiles-full-packages";
            paths = cliPackages ++ guiPackages;
          };

          dotfiles-packages = self.packages.${system}.dotfiles-cli-packages;
          default = self.packages.${system}.dotfiles-cli-packages;
          home-manager = home-manager.packages.${system}.home-manager;
        } // lib.optionalAttrs (lib.hasSuffix "-darwin" system) {
          darwin-rebuild = nix-darwin.packages.${system}.darwin-rebuild;
        };
    in
    {
      packages = forAllSystems packageSetFor;

      homeConfigurations = {
        aarch64-darwin-cli = mkHomeConfiguration {
          system = "aarch64-darwin";
          profile = "cli";
          enableGuiApps = false;
        };
        aarch64-darwin-full = mkHomeConfiguration {
          system = "aarch64-darwin";
          profile = "full";
          enableGuiApps = true;
        };
        x86_64-darwin-cli = mkHomeConfiguration {
          system = "x86_64-darwin";
          profile = "cli";
          enableGuiApps = false;
        };
        x86_64-darwin-full = mkHomeConfiguration {
          system = "x86_64-darwin";
          profile = "full";
          enableGuiApps = true;
        };
        aarch64-linux-cli = mkHomeConfiguration {
          system = "aarch64-linux";
          profile = "cli";
          enableGuiApps = false;
        };
        aarch64-linux-full = mkHomeConfiguration {
          system = "aarch64-linux";
          profile = "full";
          enableGuiApps = true;
        };
        x86_64-linux-cli = mkHomeConfiguration {
          system = "x86_64-linux";
          profile = "cli";
          enableGuiApps = false;
        };
        x86_64-linux-full = mkHomeConfiguration {
          system = "x86_64-linux";
          profile = "full";
          enableGuiApps = true;
        };
      };

      darwinConfigurations =
        lib.listToAttrs (lib.concatMap
          (system: [
            {
              name = "${system}-cli";
              value = mkDarwinConfiguration {
                inherit system;
                profile = "cli";
                enableGuiApps = false;
              };
            }
            {
              name = "${system}-full";
              value = mkDarwinConfiguration {
                inherit system;
                profile = "full";
                enableGuiApps = true;
              };
            }
          ])
          darwinSystems);
    };
}
