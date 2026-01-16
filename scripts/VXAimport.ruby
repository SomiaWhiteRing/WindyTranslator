#==============================================================================
# ★ 智能增量导入工具 (Fix Position Ver.)
#==============================================================================
# 修复说明：
# 1. 修正了注释插入位置错误的问题。
#    现在注释会插在【显示文字(101)】指令之前，而不是夹在 101 和 401 中间。
# 2. 解决了导致游戏内对话框只有头像没有文字的 BUG。
#==============================================================================

module SmartImporter
  
  INPUT_FILE = "RubyImport.txt"
  LOG_FILE   = "import_smart_log.txt"
  
  MARKER_PREFIX = "<ORIGINAL_TEXT:"
  MARKER_SUFFIX = ">"

  def self.log(msg)
    File.open(LOG_FILE, "a") { |f| f.puts "[#{Time.now.strftime('%H:%M:%S')}] #{msg}" }
  end

  def self.run
    File.open(LOG_FILE, "w") { |f| f.write("=== 开始智能导入 (修复版) ===\n") }
    
    unless File.exist?(INPUT_FILE)
      msgbox "找不到 #{INPUT_FILE}！"
      exit
    end

    begin
      log "正在读取数据..."
      raw_data = File.read(INPUT_FILE, :encoding => "UTF-8").sub("\xEF\xBB\xBF", "")
      $imported_data = eval(raw_data)
      log "数据载入成功。"
      
      do_import
      
    rescue Exception => e
      log "错误: #{e.message}\n#{e.backtrace.join("\n")}"
      msgbox "出错！请查看日志。"
    end
    exit
  end

  def self.do_import
    count = 0
    map_infos = load_data("Data/MapInfos.rvdata2")
    
    map_infos.each_key do |map_id|
      key_3 = sprintf("Map%03d.txt", map_id)
      key_4 = sprintf("Map%04d.txt", map_id)
      
      translations = nil
      if $imported_data.has_key?(key_4)
        translations = $imported_data[key_4]
      elsif $imported_data.has_key?(key_3)
        translations = $imported_data[key_3]
      end

      # 即使 translations 为空也扫描，为了支持回滚
      map_path = sprintf("Data/Map%03d.rvdata2", map_id)
      map = load_data(map_path)
      
      if process_events(map.events, translations)
        save_data(map, map_path)
        count += 1
        log "地图 #{map_id} 已更新。"
      end
    end

    common_path = "Data/CommonEvents.rvdata2"
    common_events = load_data(common_path)
    c_ev_hash = {}
    common_events.each_with_index { |ce, i| c_ev_hash[i] = ce if ce }
    
    trans_common = $imported_data["CommonEvents.txt"]
    
    if process_events(c_ev_hash, trans_common)
      final_arr = Array.new(common_events.size)
      c_ev_hash.each { |i, ce| final_arr[i] = ce }
      save_data(final_arr, common_path)
      count += 1
      log "公共事件已更新。"
    end

    msgbox "导入修复完成！\n更新了 #{count} 个文件。"
  end

  def self.process_events(events_hash, translations)
    modified = false
    events_hash.each_value do |event|
      next unless event.is_a?(RPG::Event) || event.is_a?(RPG::CommonEvent)
      pages = event.is_a?(RPG::Event) ? event.pages : [event]
      pages.each do |page|
        if update_list(page.list, translations)
          modified = true
        end
      end
    end
    return modified
  end

  def self.update_list(list, translations)
    return false if list.nil?
    list_changed = false
    
    # 倒序遍历
    i = list.size - 1
    while i >= 0
      cmd = list[i]
      
      # --- 情况 A: 文本指令 (Code 401) ---
      if cmd.code == 401
        # 1. 向上寻找第一行 401
        first_text_index = i
        while first_text_index > 0 && list[first_text_index - 1].code == 401
          first_text_index -= 1
        end
        
        # 2. ★★★ 关键修复 ★★★
        # 检查 401 前面是不是 101 (头像/背景设置)
        # 如果是，那么这一整块的起始点应该是 101，而不是 401
        block_start_index = first_text_index
        has_face_setup = false
        
        if first_text_index > 0 && list[first_text_index - 1].code == 101
          block_start_index -= 1
          has_face_setup = true
        end
        
        # 3. 检查 block_start_index 前面是否有标记
        marker_index = block_start_index - 1
        original_text = nil
        has_marker = false
        
        if marker_index >= 0 && list[marker_index].code == 108
          comment = list[marker_index].parameters[0]
          if comment.start_with?(MARKER_PREFIX)
            encoded = comment[MARKER_PREFIX.length...-1]
            begin
              original_text = decode_text(encoded)
              has_marker = true
            rescue
              log "Base64解码失败，跳过。"
            end
          end
        end
        
        # 如果没有标记，提取原文（仅从 401 部分提取）
        unless has_marker
          buffer = []
          (first_text_index..i).each do |k|
            buffer << list[k].parameters[0]
          end
          original_text = buffer.join("\n")
        end
        
        # 4. 查找翻译
        new_translation = nil
        if translations && translations[original_text] && translations[original_text]["text"]
          new_translation = translations[original_text]["text"]
        end
        
        # 5. 执行修改
        if new_translation
          new_cmds = []
          
          # A. 插入标记 (如果之前没有)
          unless has_marker
            encoded = encode_text(original_text)
            new_cmds << RPG::EventCommand.new(108, cmd.indent, ["#{MARKER_PREFIX}#{encoded}#{MARKER_SUFFIX}"])
          end
          
          # B. 插入 101 (如果有的话，保留原样)
          if has_face_setup
            new_cmds << list[block_start_index] # 直接复制原来的 101 指令对象
          end
          
          # C. 插入翻译后的 401
          new_translation.split("\n").each do |line|
            new_cmds << RPG::EventCommand.new(401, cmd.indent, [line])
          end
          
          # 计算替换范围
          # 起始点：如果有标记则从 marker_index 开始，否则从 block_start_index 开始
          # 结束点：i
          start_idx = has_marker ? marker_index : block_start_index
          len = i - start_idx + 1
          
          list[start_idx, len] = new_cmds
          list_changed = true
          
        elsif has_marker && new_translation.nil?
          # 回滚操作
          new_cmds = []
          
          # 恢复 101
          if has_face_setup
            new_cmds << list[block_start_index]
          end
          
          # 恢复原文 401
          original_text.split("\n").each do |line|
            new_cmds << RPG::EventCommand.new(401, cmd.indent, [line])
          end
          
          start_idx = marker_index
          len = i - start_idx + 1
          
          list[start_idx, len] = new_cmds
          list_changed = true
        end
        
        # 跳转指针：跳过整个处理过的块
        # 下一次循环从 block_start_index - 1 (如果没标记) 或 marker_index - 1 (如果有标记) 开始
        i = (has_marker ? marker_index : block_start_index) - 1
        
      # --- 情况 B: 选项 (Code 102) ---
      # 暂保持简单替换，不插入注释，以免破坏分支结构
      elsif cmd.code == 102
        if translations
          choices = cmd.parameters[0]
          changed = false
          new_choices = choices.map do |txt|
            if translations[txt] && translations[txt]["text"]
              changed = true
              translations[txt]["text"]
            else
              txt
            end
          end
          
          if changed
            cmd.parameters[0] = new_choices
            list_changed = true
          end
        end
        i -= 1
        
      else
        i -= 1
      end
    end
    
    return list_changed
  end

  def self.encode_text(str)
    [str].pack("m0").gsub("\n", "")
  end

  def self.decode_text(str)
    str.unpack("m0")[0].force_encoding("UTF-8")
  end

end

SmartImporter.run