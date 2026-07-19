param(
    [switch]$Write
)

$ErrorActionPreference = 'Stop'

$jsonPath = 'Works/もしもコレクション7/translated/translation_translated_proofread.json'
$ledgerPath = 'Works/もしもコレクション7/translated/darkness_review_ledger.json'

function ConvertFrom-LedgerText([string]$value) {
    return $value.Replace('\n', "`n")
}

function ConvertTo-JsonString([string]$value) {
    return ($value | ConvertTo-Json -Compress)
}

function Get-ReviewedText([string]$original, [string]$text, [string]$decision) {
    $new = $text

    if ($decision -eq 'person') {
        $new = $new.Replace('达克尼斯', '达克妮斯')
        $new = $new.Replace('达克妮丝', '达克妮斯')

        # Speaker/name labels and direct character references.
        $new = $new -replace '(^|[\n　「【>\\])暗黑Ⅲ(?=[:：】\n，、！!])', '${1}达克妮斯'
        $new = $new -replace '(^|[\n　「【>\\])暗黑(?=[:：】\n，、！!])', '${1}达克妮斯'

        foreach ($pair in @(
            @('暗黑的房间', '达克妮斯的房间'),
            @('暗黑的滑板', '达克妮斯的滑板'),
            @('暗黑那家伙', '达克妮斯那家伙'),
            @('暗黑她们', '达克妮斯她们'),
            @('暗黑她', '达克妮斯她'),
            @('暗黑也', '达克妮斯也'),
            @('暗黑和', '达克妮斯和'),
            @('暗黑比', '达克妮斯比'),
            @('暗黑托', '达克妮斯托'),
            @('暗黑从', '达克妮斯从'),
            @('暗黑那里', '达克妮斯那里'),
            @('暗黑阁下', '达克妮斯阁下'),
            @('暗黑君', '达克妮斯君'),
            @('暗黑加入', '达克妮斯加入'),
            @('暗黑成为', '达克妮斯成为'),
            @('暗黑没有受到', '达克妮斯没有受到'),
            @('暗黑受到了', '达克妮斯受到了'),
            @('暗黑不是', '达克妮斯不是'),
            @('我不是暗黑', '我不是达克妮斯'),
            @('是暗黑啊', '是达克妮斯啊'),
            @('和暗黑也', '和达克妮斯也')
        )) {
            $new = $new.Replace($pair[0], $pair[1])
        }

        # Direct vocative/name mentions. Keep lexical terms such as 暗黑魔法/暗黑系 untouched.
        $new = $new -replace '暗黑(?=(……|…|，|、|！|!|。|$))', '达克妮斯'
    }
    elseif ($decision -eq 'term') {
        foreach ($pair in @(
            @('达克妮斯炮', '暗黑炮'),
            @('达克妮丝炮', '暗黑炮'),
            @('达克尼斯炮', '暗黑炮'),
            @('黑暗射门', '暗黑射门'),
            @('黑暗幻影', '暗黑幻影'),
            @('黑暗术IV', '暗黑术IV')
        )) {
            $new = $new.Replace($pair[0], $pair[1])
        }
    }

    return $new
}

$raw = Get-Content -Raw -LiteralPath $jsonPath
$ledger = Get-Content -Raw -LiteralPath $ledgerPath | ConvertFrom-Json
$changes = New-Object System.Collections.Generic.List[object]

foreach ($row in $ledger) {
    if (-not $row.reviewed) {
        throw "Unreviewed ledger entry: $($row.id)"
    }

    $original = ConvertFrom-LedgerText $row.original
    $oldText = ConvertFrom-LedgerText $row.current_text
    $newText = Get-ReviewedText $original $oldText $row.decision

    if ($newText -eq $oldText) {
        continue
    }

    $keyJson = ConvertTo-JsonString $original
    $oldTextJson = ConvertTo-JsonString $oldText
    $newTextJson = ConvertTo-JsonString $newText

    $keyPos = $raw.IndexOf($keyJson)
    if ($keyPos -lt 0) {
        throw "Could not locate key for $($row.id) $($row.map)"
    }

    $oldTextNeedle = '"text": ' + $oldTextJson
    $newTextNeedle = '"text": ' + $newTextJson

    $absoluteTextPos = $raw.IndexOf($oldTextNeedle, $keyPos)
    if ($absoluteTextPos -lt 0) {
        throw "Could not locate text for $($row.id) $($row.map)"
    }

    $raw = $raw.Remove($absoluteTextPos, $oldTextNeedle.Length).Insert($absoluteTextPos, $newTextNeedle)

    $changes.Add([pscustomobject]@{
        id = $row.id
        decision = $row.decision
        map = $row.map
        old = $oldText.Replace("`n", '\n')
        new = $newText.Replace("`n", '\n')
    })
}

if ($Write) {
    Set-Content -LiteralPath $jsonPath -Value $raw -NoNewline -Encoding UTF8
}

[pscustomobject]@{
    write = [bool]$Write
    changes = $changes.Count
    reviewed = ($ledger | Where-Object reviewed).Count
    total = $ledger.Count
}

$changes | Select-Object -First 80 | Format-List
