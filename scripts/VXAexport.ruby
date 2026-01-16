#==============================================================================
# ★ 最终稳定版 v3：JSON 导出工具
#==============================================================================
# 修复日志：
# 1. 修复 can't modify frozen string 错误 (移除 clone，改用非破坏性替换)
# 2. 保持双斜杠修复
# 3. 保持 Speaker ID (NARRATION) 逻辑
#==============================================================================

module JsonExporter
  
  OUTPUT_FILENAME = "Exported_Dialogues.json"
  
  # 定义特殊标识符
  ID_NARRATION = "NARRATION"
  ID_SYSTEM    = "SYSTEM"

  def self.run
    full_data = {}

    # 1. 遍历地图
    map_infos = load_data("Data/MapInfos.rvdata2")
    map_infos.each_key do |map_id|
      map = load_data(sprintf("Data/Map%03d.rvdata2", map_id))
      file_key = sprintf("Map%03d.txt", map_id)
      
      events_data = extract_events(map.events)
      full_data[file_key] = events_data unless events_data.empty?
    end

    # 2. 遍历公共事件
    common_events = load_data("Data/CommonEvents.rvdata2")
    c_ev_hash = {}
    common_events.each_with_index do |ce, index|
      next if ce.nil?
      c_ev_hash[index] = ce
    end
    
    common_data = extract_events(c_ev_hash)
    full_data["CommonEvents.txt"] = common_data unless common_data.empty?

    # 3. 写入文件
    File.open(OUTPUT_FILENAME, "w") do |f|
      f.write(build_json(full_data))
    end

    msgbox "导出成功！\nSuccess!\nFile: #{OUTPUT_FILENAME}"
    exit
  end

  def self.extract_events(events_hash)
    file_dialogues = {} 

    events_hash.each_value do |event|
      if event.is_a?(RPG::Event)
        event.pages.each do |page|
          process_list(page.list, file_dialogues)
        end
      elsif event.is_a?(RPG::CommonEvent)
        process_list(event.list, file_dialogues)
      end
    end
    return file_dialogues
  end

  def self.process_list(list, collection)
    return if list.nil?

    current_face_name = ""
    current_face_index = 0
    text_buffer = [] 

    save_buffer = lambda do
      if !text_buffer.empty?
        merged_text = text_buffer.join("\n")
        
        if current_face_name.empty?
          spk_id = ID_NARRATION
        else
          spk_id = "#{current_face_name}_#{current_face_index}"
        end

        collection[merged_text] = {
          "text_to_translate" => merged_text,
          "original_marker"   => "Message",
          "speaker_id"        => spk_id
        }
        text_buffer.clear
      end
    end

    list.each do |command|
      case command.code
      when 101 # Set Face
        save_buffer.call
        current_face_name = command.parameters[0]
        current_face_index = command.parameters[1]
      when 401 # Text
        text_buffer << command.parameters[0]
      when 102 # Choice
        save_buffer.call
        choices = command.parameters[0]
        if current_face_name.empty?
          choice_spk_id = ID_NARRATION
        else
          choice_spk_id = "#{current_face_name}_#{current_face_index}"
        end
        choices.each do |choice_text|
          next if choice_text.empty?
          collection[choice_text] = {
            "text_to_translate" => choice_text,
            "original_marker"   => "Choice",
            "speaker_id"        => choice_spk_id
          }
        end
      else
        save_buffer.call
      end
    end
    save_buffer.call
  end

  def self.build_json(data)
    json = "{\n"
    file_count = 0
    total_files = data.keys.size

    data.each do |filename, dialogues|
      file_count += 1
      json << "    \"#{escape_json(filename)}\": {\n"
      
      dlg_count = 0
      total_dlgs = dialogues.keys.size
      
      dialogues.each do |original_text, info|
        dlg_count += 1
        
        json << "        \"#{escape_json(original_text)}\": {\n"
        json << "            \"text_to_translate\": \"#{escape_json(info['text_to_translate'])}\",\n"
        json << "            \"original_marker\": \"#{escape_json(info['original_marker'])}\",\n"
        json << "            \"speaker_id\": \"#{escape_json(info['speaker_id'])}\"\n"
        
        json << "        }"
        json << "," if dlg_count < total_dlgs
        json << "\n"
      end

      json << "    }"
      json << "," if file_count < total_files
      json << "\n"
    end
    json << "}"
    return json
  end

  # =======================================================
  # ★ 核心修复：更安全的转义逻辑
  # =======================================================
  def self.escape_json(str)
    return "" if str.nil?
    
    # 1. 使用 gsub 而不是 gsub!，这样会自动生成新字符串，
    #    彻底避开 frozen string (不可修改字符串) 的问题。
    
    # 先处理反斜杠 (必须第一步)
    s = str.gsub('\\') { '\\\\' }
    
    # 处理双引号
    s = s.gsub('"', '\"')
    
    # 处理控制符
    s = s.gsub("\n", '\\n')
    s = s.gsub("\r", '')
    s = s.gsub("\t", '\\t')
    
    return s
  end
end

JsonExporter.run