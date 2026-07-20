// Built against Sinflower/UberWolf v0.6.3 (commit 663dc2d) with the adjacent
// GameDat-wolf366.patch, Command-json-control-flow.patch, and
// CommonEvent-json-arguments.patch applied. The patches preserve WOLF 3.660
// Game.dat files and expose command control flow, call arguments, and raw
// CommonEvent activation metadata.
#define NOMINMAX
#define _SILENCE_CXX17_CODECVT_HEADER_DEPRECATION_WARNING

#include <filesystem>
#include <iostream>
#include <string>

#include "UberWolfLib/WolfRPG/WolfRPG.hpp"

namespace fs = std::filesystem;

namespace
{
void makeDumpDirs(const fs::path& output)
{
    fs::create_directories(output / "game");
    fs::create_directories(output / "common");
    fs::create_directories(output / "maps");
    fs::create_directories(output / "databases");
}

int dump(const fs::path& dataPath, const fs::path& output)
{
    makeDumpDirs(output);
    WolfRPG game(dataPath);
    if (!game.Valid())
        return 2;

    game.GetGameDat().ToJson(output / "game");
    game.GetCommonEvents().ToJson(output / "common");
    for (const Map& map : game.GetMaps())
        map.ToJson(output / "maps");
    for (const Database& database : game.GetDatabases())
        database.ToJson(output / "databases");

    std::cout << "maps=" << game.GetMaps().size()
              << " common_events=" << game.GetCommonEvents().GetEvents().size()
              << " databases=" << game.GetDatabases().size() << '\n';
    return 0;
}

int apply(const fs::path& dataPath, const fs::path& patches, const fs::path& output)
{
    WolfRPG game(dataPath);
    if (!game.Valid())
        return 2;

    game.GetGameDat().Patch(patches / "game");
    game.GetCommonEvents().Patch(patches / "common");
    for (Map& map : game.GetMaps())
        map.Patch(patches / "maps");
    for (Database& database : game.GetDatabases())
        database.Patch(patches / "databases");

    game.Save2File(output);
    WolfRPG verification(output);
    if (!verification.Valid())
        return 3;

    std::cout << "verified maps=" << verification.GetMaps().size()
              << " common_events=" << verification.GetCommonEvents().GetEvents().size()
              << " databases=" << verification.GetDatabases().size() << '\n';
    return 0;
}
} // namespace

int wmain(int argc, wchar_t** argv)
{
    try
    {
        if (argc == 4 && std::wstring(argv[1]) == L"dump")
            return dump(argv[2], argv[3]);
        if (argc == 5 && std::wstring(argv[1]) == L"apply")
            return apply(argv[2], argv[3], argv[4]);

        std::wcerr << L"Usage:\n"
                   << L"  WolfRPGText dump DATA_DIR JSON_DIR\n"
                   << L"  WolfRPGText apply DATA_DIR JSON_DIR OUTPUT_DATA_DIR\n";
        return 1;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << '\n';
        return 4;
    }
}
